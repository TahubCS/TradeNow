"""Offline scenario and fault-injection checks for the research workflow."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from io import StringIO
from pathlib import Path

from .execution import ApprovedOrder, SimulatedBroker
from .market_data import parse_bars
from .offline import run_offline
from .simulation import Config, simulate
from .synthetic import bars_to_csv, generate_bars, validate_synthetic_bars


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _expect_error(action: Callable[[], object], error_type: type[Exception]) -> None:
    try:
        action()
    except error_type:
        return
    raise AssertionError(f"Expected {error_type.__name__}")


def audit_simulation(result: dict) -> None:
    fills = result["fills"]
    curve = result["equity_curve"]
    _require(bool(curve), "Equity curve is empty")
    _require(curve[-1]["equity"] == result["ending_equity"], "Ending equity differs from curve")
    _require(Decimal(result["ending_equity"]) - Decimal(result["starting_cash"])
             == Decimal(result["total_pnl"]), "P&L does not reconcile")
    _require(len({fill["order_id"] for fill in fills}) == len(fills), "Duplicate order fill")

    approved = {(item["date"], item["action"]) for item in result["risk_decisions"]
                if item["approved"]}
    position = 0
    position_contract = None
    for fill in fills:
        _require((fill["date"], fill["side"]) in approved, "Fill lacks risk approval")
        if fill["side"] == "BUY":
            position += 1
            position_contract = fill["contract"]
        else:
            _require(fill["contract"] == position_contract,
                     "Exit contract differs from entry")
            position -= 1
            position_contract = None
        _require(position in (0, 1), "Position left the allowed range")
    _require(position == result["open_contracts"], "Fill ledger disagrees with position")
    _require(position_contract == result["open_contract"],
             "Open contract differs from fill ledger")
    _require(sum(fill["side"] == "SELL" for fill in fills)
             == len(result["closed_trades"]), "Closed trade count disagrees with fills")

    halts = [item["date"] for item in result["risk_decisions"]
             if item["reason"] == "DRAWDOWN_LIMIT"]
    if halts:
        halt_date = halts[0]
        _require(not any(fill["side"] == "BUY" and fill["date"] > halt_date
                         for fill in fills), "New entry after drawdown halt")
        if halt_date < curve[-1]["date"]:
            _require(position == 0, "Position was not closed after drawdown halt")


def _missing_weekday() -> None:
    bars = generate_bars(days=180)
    del bars[20]
    _expect_error(lambda: validate_synthetic_bars(bars), ValueError)


def _price_outlier() -> None:
    bars = generate_bars(days=180)
    bars[20] = replace(bars[20], close=bars[20].close * 2,
                       high=bars[20].close * 2 + 1)
    _expect_error(lambda: validate_synthetic_bars(bars), ValueError)


def _duplicate_bar() -> None:
    bars = generate_bars(days=180)
    text = bars_to_csv(bars + [bars[-1]])
    _expect_error(lambda: parse_bars(StringIO(text)), ValueError)


def _order_retry() -> None:
    broker = SimulatedBroker(Decimal("100000"), Decimal("10"),
                             Decimal("0.10"), Decimal("1.50"))
    order = ApprovedOrder("retry-1", "2026-09-10", "BUY", Decimal("2000"))
    broker.submit(order)
    broker.submit(order)
    _require(len(broker.fills) == 1 and broker.cash == Decimal("99998.50"),
             "Order retry caused a second fill or fee")
    _expect_error(lambda: broker.submit(replace(order, opening_price=Decimal("2001"))),
                  ValueError)


def _reconciliation_failure() -> None:
    broker = SimulatedBroker(Decimal("100000"), Decimal("10"),
                             Decimal("0.10"), Decimal("1.50"))
    broker.submit(ApprovedOrder("reconcile-1", "2026-09-10", "BUY", Decimal("2000")))
    broker.fills.clear()
    _expect_error(broker.reconcile, RuntimeError)


def _notional_rejection() -> None:
    result = simulate(generate_bars(days=180),
                      Config(max_notional_fraction=Decimal("0.01")))
    _require(not result["fills"], "Oversized proposal reached simulated broker")
    _require(any(item["reason"] == "NOTIONAL_LIMIT" for item in result["risk_decisions"]),
             "Oversized proposal was not explicitly rejected")


def _gap_drawdown() -> None:
    bars = generate_bars(days=180)
    baseline = simulate(bars)
    first_buy = next(fill for fill in baseline["fills"] if fill["side"] == "BUY")
    buy_index = next(index for index, bar in enumerate(bars)
                     if bar.date.isoformat() == first_buy["date"])
    gap_index = buy_index + 1
    gap_price = bars[gap_index].open * Decimal("0.80")
    bars[gap_index] = replace(bars[gap_index], open=gap_price, close=gap_price,
                              high=gap_price + 1, low=gap_price - 1)
    result = simulate(bars, Config(max_drawdown_fraction=Decimal("0.01")))
    audit_simulation(result)
    _require(result["halted"], "Large adverse gap did not halt entries")
    halt_dates = [item["date"] for item in result["risk_decisions"]
                  if item["reason"] == "DRAWDOWN_LIMIT"]
    _require(halt_dates == [bars[gap_index].date.isoformat()],
             "Drawdown halt did not occur on the injected gap")
    _require(any(fill["side"] == "SELL" and
                 fill["date"] == bars[gap_index + 1].date.isoformat()
                 for fill in result["fills"]), "Position was not closed at the next open")


FAULT_CASES = (
    ("missing_weekday", _missing_weekday),
    ("price_outlier", _price_outlier),
    ("duplicate_bar", _duplicate_bar),
    ("order_retry", _order_retry),
    ("reconciliation_failure", _reconciliation_failure),
    ("notional_rejection", _notional_rejection),
    ("gap_drawdown", _gap_drawdown),
)


def run_stress(seeds: int = 12, days: int = 360) -> dict:
    if seeds < 1:
        raise ValueError("Stress run requires at least one seed")
    if days < 180:
        raise ValueError("Stress run requires at least 180 bars per seed")

    seed_runs = []
    for seed in range(seeds):
        try:
            report, csv_text = run_offline(seed, days)
            replayed_report, replayed_csv = run_offline(seed, days)
            _require((report, csv_text) == (replayed_report, replayed_csv),
                     "Repeated run was not deterministic")
            for hypothesis in report["research"]["hypotheses"]:
                audit_simulation(hypothesis["development"])
                audit_simulation(hypothesis["validation"])
            holdout = report["research"]["holdout_result"]
            audit_simulation(holdout)
            if report["research"]["selected_hypothesis"] is None:
                _require(not holdout["fills"], "No-trade gate still submitted orders")
            seed_runs.append({"seed": seed, "passed": True, "run_id": report["run_id"],
                              "selected_hypothesis": report["research"]["selected_hypothesis"],
                              "holdout_pnl": holdout["total_pnl"],
                              "holdout_fills": len(holdout["fills"])})
        except Exception as error:
            seed_runs.append({"seed": seed, "passed": False,
                              "error": f"{type(error).__name__}: {error}"})

    fault_cases = []
    for name, check in FAULT_CASES:
        try:
            check()
            fault_cases.append({"name": name, "passed": True})
        except Exception as error:
            fault_cases.append({"name": name, "passed": False,
                                "error": f"{type(error).__name__}: {error}"})

    passed = all(item["passed"] for item in seed_runs + fault_cases)
    successful = [item for item in seed_runs if item["passed"]]
    coverage = {"strategies_selected": sum(item["selected_hypothesis"] is not None
                                            for item in successful),
                "no_trade_selections": sum(item["selected_hypothesis"] is None
                                           for item in successful),
                "positive_holdouts": sum(Decimal(item["holdout_pnl"]) > 0
                                         for item in successful),
                "negative_holdouts": sum(Decimal(item["holdout_pnl"]) < 0
                                         for item in successful)}
    code_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    identity = {"seeds": seeds, "days": days, "code_sha256": code_hash,
                "seed_run_ids": [item.get("run_id") for item in seed_runs]}
    run_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    return {"schema_version": 1, "mode": "offline_stress", "run_id": run_id,
            "code_sha256": code_hash, "seeds": seeds, "days_per_seed": days,
            "passed": passed, "coverage": coverage,
            "seed_runs": seed_runs, "fault_cases": fault_cases,
            "limits": "Synthetic-only checks; passing does not validate real MGC trading"}


def save_stress(report: dict, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"stress-{report['run_id']}.json"
    markdown_path = output_dir / f"stress-{report['run_id']}.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")

    lines = ["# Offline stress report", "", f"Run ID: `{report['run_id']}`  ",
             f"Overall: **{'PASS' if report['passed'] else 'FAIL'}**  ",
             f"Seeds: {report['seeds']}; bars per seed: {report['days_per_seed']}  ",
             f"Selections: {report['coverage']['strategies_selected']} strategies, "
             f"{report['coverage']['no_trade_selections']} no-trade decisions", "",
             "| Seed | Status | Selected | Holdout P&L | Fills |",
             "| ---: | --- | --- | ---: | ---: |"]
    for item in report["seed_runs"]:
        lines.append(f"| {item['seed']} | {'PASS' if item['passed'] else 'FAIL'} | "
                     f"{item.get('selected_hypothesis') or 'NO TRADE'} | "
                     f"{item.get('holdout_pnl', '—')} | {item.get('holdout_fills', '—')} |")
    lines.extend(["", "## Fault injections", ""])
    for item in report["fault_cases"]:
        lines.append(f"- {'PASS' if item['passed'] else 'FAIL'}: {item['name']}"
                     + (f" — {item['error']}" if not item["passed"] else ""))
    lines.extend(["", report["limits"], ""])
    markdown_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return json_path, markdown_path
