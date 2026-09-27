"""Run an offline research cycle with generated or locally supplied bars."""

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from io import StringIO
from pathlib import Path

from .market_data import Bar, parse_bars
from .simulation import Config, simulate
from .synthetic import bars_to_csv, generate_bars, validate_synthetic_bars


CANDIDATES = (("sma_3_10", 3, 10), ("sma_5_20", 5, 20), ("sma_10_30", 10, 30))
MAX_LOCAL_CSV_BYTES = 1_000_000
MAX_LOCAL_BARS = 5_000


def _summary(result: dict) -> dict:
    trades = result["closed_trades"]
    winning_trades = sum(Decimal(trade["net_pnl"]) > 0 for trade in trades)
    win_rate = (None if not trades else
                str((Decimal(winning_trades) * 100 / len(trades)).quantize(Decimal("0.001"))))
    exposure = (Decimal(sum(day["position_contracts"] for day in result["equity_curve"]))
                / len(result["equity_curve"]) * 100).quantize(Decimal("0.001"))
    return {
        "total_pnl": result["total_pnl"],
        "total_return_pct": result["total_return_pct"],
        "max_drawdown_pct": result["max_drawdown_pct"],
        "closed_trades": len(trades),
        "win_rate_pct": win_rate,
        "exposure_pct": str(exposure),
        "fills": len(result["fills"]),
        "rejected_orders": sum(not decision["approved"] and
                               decision["action"] != "HALT_NEW_ENTRIES"
                               for decision in result["risk_decisions"]),
        "no_trade_days": sum(proposal["action"] == "NO_TRADE"
                             for proposal in result["proposals"]),
        "halted": result["halted"],
        "open_contracts": result["open_contracts"],
    }


def run_offline(seed: int = 3, days: int = 360) -> tuple[dict, str]:
    csv_text = bars_to_csv(generate_bars(seed, days))
    bars = parse_bars(StringIO(csv_text))
    validate_synthetic_bars(bars)
    return _run_research(bars, csv_text.encode("utf-8"), "seeded_synthetic", seed), csv_text


def run_local_csv(source_bytes: bytes, filename: str = "local.csv") -> tuple[dict, bytes]:
    """Research one local contract; keep its original bytes for exact provenance."""
    if len(source_bytes) > MAX_LOCAL_CSV_BYTES:
        raise ValueError(f"Local CSV exceeds {MAX_LOCAL_CSV_BYTES} bytes")
    try:
        csv_text = source_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Local CSV must be UTF-8 encoded") from error
    bars = parse_bars(StringIO(csv_text))
    if len(bars) < 180:
        raise ValueError("Offline research requires at least 180 bars")
    if len(bars) > MAX_LOCAL_BARS:
        raise ValueError(f"Local CSV exceeds {MAX_LOCAL_BARS} bars")
    if not bars[0].contract.upper().startswith("MGC"):
        raise ValueError("Local CSV must contain one MGC contract")
    basename = filename.replace("\\", "/").split("/")[-1]
    safe_name = "".join(char if char.isalnum() or char in "._- " else "_"
                        for char in basename)[:100] or "local.csv"
    return _run_research(bars, source_bytes, "local_csv", source_name=safe_name), source_bytes


def _run_research(bars: list[Bar], source_bytes: bytes, source: str,
                  seed: int | None = None, source_name: str | None = None) -> dict:
    days = len(bars)
    development_end = days * 3 // 5
    validation_end = days * 4 // 5
    periods = {
        "development": bars[:development_end],
        "validation": bars[development_end:validation_end],
        "holdout": bars[validation_end:],
    }

    hypotheses = []
    best_score: Decimal | None = None
    best_config: Config | None = None
    best_name: str | None = None
    for name, fast, slow in CANDIDATES:
        config = replace(Config(), fast_window=fast, slow_window=slow)
        development = simulate(periods["development"], config)
        validation = simulate(periods["validation"], config)
        score = (Decimal(validation["total_return_pct"])
                 - Decimal(validation["max_drawdown_pct"]))
        hypotheses.append({
            "name": name,
            "parameters": {"fast_window": fast, "slow_window": slow},
            "development": development,
            "validation": validation,
            "validation_score": str(score),
        })
        if best_score is None or score > best_score:
            best_score, best_config, best_name = score, config, name

    assert best_score is not None and best_config is not None
    selected = best_name if best_score > 0 else None
    test_config = replace(best_config, enable_entries=selected is not None)
    holdout = simulate(periods["holdout"], test_config)
    data_hash = hashlib.sha256(source_bytes).hexdigest()
    code_digest = hashlib.sha256()
    for name in ("market_data.py", "synthetic.py", "simulation.py", "execution.py", "offline.py"):
        code_digest.update(name.encode("utf-8"))
        code_digest.update((Path(__file__).parent / name).read_bytes())
    code_hash = code_digest.hexdigest()
    run_inputs = {"schema_version": 1, "source": source, "source_name": source_name,
                  "seed": seed, "days": days,
                  "data_hash": data_hash, "code_hash": code_hash, "candidates": CANDIDATES,
                  "config": {key: str(value) for key, value in vars(Config()).items()}}
    run_id = hashlib.sha256(json.dumps(run_inputs, sort_keys=True).encode()).hexdigest()[:16]

    report = {
        "schema_version": 1,
        "mode": "offline_simulation",
        "run_id": run_id,
        "code_sha256": code_hash,
        "data": {"source": source, "source_name": source_name,
                 "contract": bars[0].contract, "seed": seed,
                 "bars": days, "sha256": data_hash,
                 "calendar": ("weekdays_only_no_exchange_holidays" if seed is not None
                              else "not_validated"),
                 "periods": {name: {"first_date": part[0].date.isoformat(),
                                    "last_date": part[-1].date.isoformat(), "bars": len(part)}
                             for name, part in periods.items()}},
        "research": {"hypotheses": hypotheses,
                     "selection_rule": "highest validation return pct minus maximum drawdown pct; require score > 0",
                     "selected_hypothesis": selected,
                     "best_validation_score": str(best_score),
                     "holdout_result": holdout,
                     "holdout_summary": _summary(holdout)},
        "limits": (["fictional prices"] if source == "seeded_synthetic" else
                   ["local source accuracy and licensing not verified"]) +
                  ["one nonexpiring contract", "daily bars only",
                   "no futures margin or exchange calendar", "simulated fills only"],
    }
    return report


def render_markdown(report: dict) -> str:
    research = report["research"]
    data = report["data"]
    rows = [
        "# Offline research report",
        "",
        f"Run ID: `{report['run_id']}`  ",
        (f"Data: {data['bars']} generated `{data['contract']}` bars, seed {data['seed']}  "
         if data["source"] == "seeded_synthetic" else
         f"Data: {data['bars']} local `{data['contract']}` bars from `{data['source_name']}`  "),
        f"Data SHA-256: `{data['sha256']}`  ",
        f"Code SHA-256: `{report['code_sha256']}`  ",
        f"Selected hypothesis: **{research['selected_hypothesis'] or 'NO TRADE'}**",
        "",
        "The candidates were fixed before evaluation. Development and validation are",
        "chronological periods. Only validation scores choose the strategy; the holdout",
        "period is reported after selection. Each period starts flat.",
        "",
        "| Hypothesis | Development P&L | Validation P&L | Validation score |",
        "| --- | ---: | ---: | ---: |",
    ]
    for hypothesis in research["hypotheses"]:
        rows.append(f"| {hypothesis['name']} | "
                    f"{hypothesis['development']['total_pnl']} | "
                    f"{hypothesis['validation']['total_pnl']} | "
                    f"{hypothesis['validation_score']} |")

    summary = research["holdout_summary"]
    rows.extend([
        "",
        "## Holdout",
        "",
        f"- Total P&L (including any open position marked at the last close): {summary['total_pnl']} simulated USD",
        f"- Return: {summary['total_return_pct']}%",
        f"- Maximum drawdown: {summary['max_drawdown_pct']}%",
        f"- Closed trades: {summary['closed_trades']}; fills: {summary['fills']}",
        f"- Exposure: {summary['exposure_pct']}%; open contracts at end: {summary['open_contracts']}",
        f"- Risk halt: {summary['halted']}; rejected orders: {summary['rejected_orders']}",
        "",
        "## Limits",
        "",
        ("These prices are fictional." if data["source"] == "seeded_synthetic" else
         "Local file provenance is recorded, but its accuracy and licensing are not verified."),
        "The simulator treats the contract as nonexpiring. Futures margin,",
        "exchange holidays, intraday stops, and brokerage behavior are not modeled.",
        "No external account was contacted or charged.",
        "",
    ])
    return "\n".join(rows)


def save_offline(report: dict, csv_text: str | bytes, output_dir: Path) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = report["run_id"]
    bars_path = output_dir / f"bars-{run_id}.csv"
    report_path = output_dir / f"report-{run_id}.json"
    markdown_path = output_dir / f"report-{run_id}.md"
    bars_path.write_bytes(csv_text if isinstance(csv_text, bytes) else csv_text.encode("utf-8"))
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    markdown_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return bars_path, report_path, markdown_path
