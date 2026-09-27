"""The live-trading gate (ADR-008): beat plain buy-and-hold before real money.

Every threshold lives here, so the rule is enforced in one place. The gate only
reports a verdict; nothing in this system can trade live, whatever it says.
Nothing here performs I/O.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal


# Research stage: rolling test windows selected only from earlier bars.
MIN_WINDOW_BEAT_FRACTION = Decimal("0.60")
MIN_CLOSED_TRADES = 30
# Forward stage: paper trading after the strategy is frozen.
MIN_FORWARD_SESSIONS = 252
MAX_COST_VS_SIMULATION_BPS = Decimal("10")
MIN_FORWARD_FILLS = 10

PASS, FAIL = "PASS", "FAIL"


@dataclass(frozen=True)
class EquitySnapshot:
    """Paper equity at one session's close, with the strategy version that held it."""
    date: date
    equity: Decimal
    strategy_version: str


def strategy_version(code_sha256: str, selected_hypothesis: str | None,
                     risk_sha256: str) -> str:
    """Changing the code, the selected rule, or the risk settings restarts the forward count."""
    body = json.dumps([code_sha256, selected_hypothesis, risk_sha256])
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def _check(name: str, passed: bool, detail: str) -> dict:
    return {"check": name, "passed": passed, "detail": detail}


def research_gate(evaluation: dict, candidates: int | None = None) -> dict:
    """R1-R4 from a GLD evaluation's rolling windows. The viewed holdout is ignored.

    candidates is how many strategies the rolling selection chose among; the
    more there are, the likelier a pass is luck, which the forward stage checks."""
    summary = evaluation["rolling_pre_holdout"]["summary"]
    stressed = evaluation["rolling_pre_holdout_stressed"]["summary"]
    windows = summary["windows"]
    strategy = Decimal(summary["compounded_return_pct"])
    hold = Decimal(summary["compounded_buy_hold_100pct_return_pct"])
    beat = summary["beat_buy_hold_50pct_windows"]
    needed = (MIN_WINDOW_BEAT_FRACTION * windows).to_integral_value(rounding=ROUND_CEILING)
    stressed_strategy = Decimal(stressed["compounded_return_pct"])
    stressed_hold = Decimal(stressed["compounded_buy_hold_100pct_return_pct"])
    checks = [
        _check("R1_BEATS_FULL_BUY_AND_HOLD", windows > 0 and strategy > hold,
               f"compounded {strategy}% vs 100% buy-and-hold {hold}% over {windows} windows"),
        _check("R2_BEATS_CONSISTENTLY", windows > 0 and beat >= needed,
               f"beat same-exposure buy-and-hold in {beat} of {windows} windows "
               f"(needs {needed})"),
        _check("R3_ENOUGH_TRADES", summary["closed_trades"] >= MIN_CLOSED_TRADES,
               f"{summary['closed_trades']} closed trades (needs {MIN_CLOSED_TRADES})"),
        _check("R4_SURVIVES_COSTS", windows > 0 and stressed_strategy > stressed_hold,
               f"at ${evaluation['rolling_pre_holdout_stressed']['slippage_per_share']}/share "
               f"slippage: {stressed_strategy}% vs {stressed_hold}%"),
    ]
    return {"stage": "research", "passed": all(item["passed"] for item in checks),
            "candidates_evaluated": candidates, "checks": checks}


def forward_run(history: list[EquitySnapshot]) -> list[EquitySnapshot]:
    """The latest unbroken run of sessions under one strategy version, one per date."""
    by_date = {snapshot.date: snapshot for snapshot in history}
    ordered = [by_date[day] for day in sorted(by_date)]
    run: list[EquitySnapshot] = []
    for snapshot in reversed(ordered):
        if run and snapshot.strategy_version != run[-1].strategy_version:
            break
        run.append(snapshot)
    return list(reversed(run))


def _max_drawdown(values: list[Decimal]) -> Decimal:
    peak, worst = values[0], Decimal(0)
    for value in values:
        peak = max(peak, value)
        worst = max(worst, (peak - value) / peak)
    return worst


def _pct(value: Decimal) -> Decimal:
    return (value * 100).quantize(Decimal("0.001"))


def forward_gate(history: list[EquitySnapshot], closes: dict[date, Decimal],
                 quality_summary: dict) -> dict:
    """F1-F4 from paper equity snapshots, GLD closes, and execution quality."""
    run = [item for item in forward_run(history) if item.date in closes]
    sessions = len(run)
    checks = [_check("F1_LONG_ENOUGH", sessions >= MIN_FORWARD_SESSIONS,
                     f"{sessions} paper sessions on this strategy version "
                     f"(needs {MIN_FORWARD_SESSIONS})")]
    if sessions >= 2:
        equity = [item.equity for item in run]
        prices = [closes[item.date] for item in run]
        paper_return = equity[-1] / equity[0] - 1
        hold_return = prices[-1] / prices[0] - 1
        paper_drawdown, hold_drawdown = _max_drawdown(equity), _max_drawdown(prices)
        checks += [
            _check("F2_BEATS_FULL_BUY_AND_HOLD", paper_return > hold_return,
                   f"paper {_pct(paper_return)}% vs 100% buy-and-hold {_pct(hold_return)}%"),
            _check("F3_NO_WORSE_RISK", paper_drawdown <= hold_drawdown,
                   f"paper max drawdown {_pct(paper_drawdown)}% vs buy-and-hold "
                   f"{_pct(hold_drawdown)}%")]
    else:
        checks += [_check("F2_BEATS_FULL_BUY_AND_HOLD", False, "fewer than 2 paper sessions"),
                   _check("F3_NO_WORSE_RISK", False, "fewer than 2 paper sessions")]
    outcomes = quality_summary.get("outcomes", {})
    fills = outcomes.get("FILLED", 0) + outcomes.get("PARTIAL", 0)
    cost = quality_summary.get("mean_slippage_bps", {}).get("simulated")
    cost_ok = (fills >= MIN_FORWARD_FILLS and cost is not None
               and Decimal(str(cost)) <= MAX_COST_VS_SIMULATION_BPS)
    checks.append(_check("F4_COSTS_AS_MODELED", cost_ok,
                         f"{fills} fills (needs {MIN_FORWARD_FILLS}); mean cost vs simulated "
                         f"fill {cost if cost is not None else 'n/a'} bps "
                         f"(max {MAX_COST_VS_SIMULATION_BPS})"))
    return {"stage": "forward", "passed": all(item["passed"] for item in checks),
            "strategy_version": run[-1].strategy_version if run else None,
            "first_session": run[0].date if run else None, "checks": checks}


def verdict(research: dict, forward: dict | None) -> dict:
    """PASS only when both stages pass. A PASS permits considering live trading."""
    stages = [research] + ([forward] if forward is not None else [])
    passed = forward is not None and all(stage["passed"] for stage in stages)
    failing = [item["check"] for stage in stages for item in stage["checks"]
               if not item["passed"]]
    if forward is None:
        failing.append("FORWARD_STAGE_NOT_EVALUATED")
    return {"verdict": PASS if passed else FAIL, "failing_checks": failing,
            "research": research, "forward": forward,
            "meaning": ("Passed ADR-008. Live trading may be considered, with minimal capital "
                        "and a recorded decision; nothing is enabled automatically."
                        if passed else
                        "Not proven to beat buy-and-hold. Do not trade real money; an index "
                        "fund or simply holding GLD is expected to do better.")}
