"""Offline GLD research on locally saved Tiingo end-of-day bars."""

import csv
import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path

from .equity import EquityBar, EquityConfig, simulate_equity, validate_equity_bars
from .offline import CANDIDATES, evaluate_candidates
from .tiingo import PRIVATE_DIR


MAX_GLD_CSV_BYTES = 2_000_000
MAX_GLD_BARS = 10_000
REQUIRED = {"date", "symbol", "open", "high", "low", "close", "volume",
            "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume",
            "div_cash", "split_factor"}


def _bars_from_csv(source_bytes: bytes, config: EquityConfig) -> list[EquityBar]:
    if len(source_bytes) > MAX_GLD_CSV_BYTES:
        raise ValueError(f"GLD CSV exceeds {MAX_GLD_CSV_BYTES} bytes")
    try:
        text = source_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("GLD CSV must be UTF-8 encoded") from None
    reader = csv.DictReader(StringIO(text))
    if not reader.fieldnames or not REQUIRED.issubset(reader.fieldnames):
        raise ValueError("GLD CSV must contain Tiingo raw and adjusted daily columns")
    bars = []
    for row_number, row in enumerate(reader, start=2):
        try:
            raw = [Decimal(row[field]) for field in ("open", "high", "low", "close")]
            adjusted = [Decimal(row[field]) for field in
                        ("adj_open", "adj_high", "adj_low", "adj_close")]
            volume = int(row["volume"])
            adj_volume = int(row["adj_volume"])
            dividend = Decimal(row["div_cash"])
            split = Decimal(row["split_factor"])
            bar = EquityBar(date.fromisoformat(row["date"]), row["symbol"],
                            *raw, volume)
        except (TypeError, ValueError, InvalidOperation):
            raise ValueError(f"Invalid GLD CSV row {row_number}") from None
        if (any(not value.is_finite() for value in adjusted + [dividend, split])
                or adj_volume < 0):
            raise ValueError(f"Invalid adjusted GLD values on row {row_number}")
        if (dividend != 0 or split != 1 or adjusted != raw or adj_volume != volume):
            raise ValueError("GLD corporate actions or adjusted prices differ; "
                             "share accounting for them is not implemented")
        bars.append(bar)
        if len(bars) > MAX_GLD_BARS:
            raise ValueError(f"GLD CSV exceeds {MAX_GLD_BARS} bars")
    if len(bars) < 180:
        raise ValueError("GLD research requires at least 180 daily bars")
    validate_equity_bars(bars, config)
    return bars


def _summary(result: dict) -> dict:
    trades = result["closed_trades"]
    wins = sum(Decimal(trade["net_pnl"]) > 0 for trade in trades)
    exposure = Decimal(sum(day["position_shares"] > 0 for day in result["equity_curve"]))
    return {"total_pnl": result["total_pnl"],
            "total_return_pct": result["total_return_pct"],
            "max_drawdown_pct": result["max_drawdown_pct"],
            "closed_trades": len(trades),
            "win_rate_pct": (None if not trades else
                             str((Decimal(wins) * 100 / len(trades)).quantize(Decimal("0.001")))),
            "exposure_pct": str((exposure * 100 / len(result["equity_curve"]))
                                .quantize(Decimal("0.001"))),
            "fills": len(result["fills"]),
            "unfilled_orders": len(result["unfilled_orders"]),
            "rejected_orders": sum(not item["approved"] and
                                   item["action"] != "HALT_NEW_ENTRIES"
                                   for item in result["risk_decisions"]),
            "no_trade_days": sum(item["action"] == "NO_TRADE"
                                 for item in result["proposals"]),
            "warmup_bars": len(result["equity_curve"]) - len(result["signals"]),
            "halted": result["halted"], "open_shares": result["open_shares"]}


def run_gld_csv(source_bytes: bytes, filename: str = "GLD.csv",
                config: EquityConfig = EquityConfig(),
                source: str = "local_gld_csv") -> dict:
    bars = _bars_from_csv(source_bytes, config)
    periods, hypotheses, selected, best_score, holdout = evaluate_candidates(
        bars, config, simulate_equity)
    data_hash = hashlib.sha256(source_bytes).hexdigest()
    code_digest = hashlib.sha256()
    for name in ("equity.py", "gld_research.py", "offline.py"):
        code_digest.update(name.encode("utf-8"))
        code_digest.update((Path(__file__).parent / name).read_bytes())
    code_hash = code_digest.hexdigest()
    basename = Path(filename.replace("\\", "/")).name
    safe_name = "".join(char if char.isalnum() or char in "._- " else "_"
                        for char in basename)[:100] or "GLD.csv"
    run_inputs = {"schema_version": 1, "symbol": "GLD", "source": source,
                  "data_hash": data_hash, "code_hash": code_hash,
                  "candidates": CANDIDATES,
                  "config": {key: str(value) for key, value in vars(config).items()}}
    run_id = hashlib.sha256(json.dumps(run_inputs, sort_keys=True).encode()).hexdigest()[:16]
    return {"schema_version": 1, "mode": "offline_gld_simulation",
            "run_id": run_id, "code_sha256": code_hash,
            "config": {key: str(value) for key, value in vars(config).items()},
            "data": {"source": source, "source_name": safe_name, "symbol": "GLD",
                     "bars": len(bars), "sha256": data_hash, "price_basis": "raw",
                     "first_date": bars[0].date.isoformat(),
                     "last_date": bars[-1].date.isoformat(),
                     "periods": {name: {"first_date": part[0].date.isoformat(),
                                        "last_date": part[-1].date.isoformat(),
                                        "bars": len(part)} for name, part in periods.items()}},
            "research": {"hypotheses": hypotheses,
                         "selection_rule": "highest validation return pct minus maximum drawdown pct; require score > 0",
                         "selected_hypothesis": selected,
                         "best_validation_score": str(best_score),
                         "holdout_result": holdout,
                         "holdout_summary": _summary(holdout)},
            "limits": ["provider prices are not independently verified",
                       "daily bars cannot verify intraday execution or market impact",
                       "simulated fills and slippage only", "cash-funded long positions only"]}


def latest_imported_gld() -> tuple[bytes, str]:
    """Load the newest private import only if its manifest matches the CSV."""
    manifests = [path for path in PRIVATE_DIR.glob("GLD-*.manifest.json")
                 if re.fullmatch(r"GLD-\d{8}-\d{8}\.manifest\.json", path.name)]
    if not manifests:
        raise ValueError("No private GLD import found; run tiingo-import first")
    manifest_path = max(manifests, key=lambda path: (path.name[13:21], path.name[4:12]))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("provider") != "Tiingo" or manifest.get("symbol") != "GLD":
        raise ValueError("GLD import manifest is invalid")
    csv_path = manifest_path.with_name(manifest_path.name.replace(".manifest.json", ".csv"))
    if csv_path.stat().st_size > MAX_GLD_CSV_BYTES:
        raise ValueError("Private GLD import exceeds size limit")
    source_bytes = csv_path.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != manifest.get("bars_sha256"):
        raise ValueError("Private GLD CSV does not match its import manifest")
    return source_bytes, csv_path.name


def render_gld_markdown(report: dict) -> str:
    data = report["data"]
    research = report["research"]
    summary = research["holdout_summary"]
    return "\n".join([
        "# GLD offline research report", "",
        f"Run ID: `{report['run_id']}`  ",
        f"Source: {data['source']} / `{data['source_name']}`  ",
        f"Bars: {data['bars']} ({data['first_date']} to {data['last_date']})  ",
        f"Data SHA-256: `{data['sha256']}`  ",
        f"Code SHA-256: `{report['code_sha256']}`  ",
        f"Selected: **{research['selected_hypothesis'] or 'NO TRADE'}**", "",
        "The fixed candidates were selected using chronological validation only.",
        "The holdout was not used to choose a strategy. Each period starts flat.", "",
        "## Holdout", "",
        f"- P&L: {summary['total_pnl']} simulated USD",
        f"- Return: {summary['total_return_pct']}%",
        f"- Maximum drawdown: {summary['max_drawdown_pct']}%",
        f"- Closed trades: {summary['closed_trades']}; fills: {summary['fills']}",
        f"- Exposure: {summary['exposure_pct']}%; open shares: {summary['open_shares']}",
        f"- Rejected orders: {summary['rejected_orders']}; halted: {summary['halted']}", "",
        "Raw GLD prices are used for signals, fills, and marking equity.",
        "Cash-funded whole-share positions only; no futures margin or rolls.",
        "Daily bars and simulated fills do not establish live execution quality.",
        "No brokerage or market-data API was contacted for this replay.", "",
    ])


def save_gld_report(report: dict, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"report-{report['run_id']}"
    json_path = output_dir / f"{stem}.json"
    md_path = output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_gld_markdown(report), encoding="utf-8")
    return json_path, md_path
