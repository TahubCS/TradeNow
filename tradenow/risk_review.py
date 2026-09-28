"""Risk-adjusted review of the registered studies (ADR-014, information only).

Each study's rolling test windows are re-run unchanged and must reproduce the
rolling summary in the experiment log exactly; otherwise the review stops.
The daily curves of each process and benchmark are chained end to end, cash
is treated two ways (0%, or SHY's daily return), and each benchmark is also
held at the process's volatility, the rest in cash. The answers say whether a
process beat those risk-matched benchmarks and had a higher Sharpe ratio.

Nothing here reads files or the clock, and the review never writes to the
experiment log: it tests nothing new and can never unlock trading.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, Decimal

from .equity_types import EquityBar
from .gld_evaluation import ROLL_DEVELOPMENT_BARS, ROLL_TEST_BARS, ROLL_VALIDATION_BARS
from .metrics import max_drawdown
from .multi_evaluation import (
    PortfolioCandidate,
    rolling_runs,
    rolling_summary,
    universe_rows,
)
from .portfolio import PortfolioConfig
from .selection import (
    RESEARCH_CONFIG,
    chronological_split,
    history_rows,
    run_selected,
    select_candidate,
)
from .strategies import GLD_CANDIDATES
from .universe import Universe


TRADING_DAYS = Decimal(252)
ROOT_TRADING_DAYS = TRADING_DAYS.sqrt()


class NotReproduced(ValueError):
    """A study's re-run differs from its logged result; the review stops."""


@dataclass(frozen=True)
class Curve:
    """Daily returns of runs chained end to end, with the cash share of equity
    at each previous close (for crediting interest on idle cash)."""
    dates: list[date]
    returns: list[Decimal]
    cash_before: list[Decimal]


def chain(runs: Sequence[dict]) -> Curve:
    """Runs with `starting_cash` and an equity curve of dated `equity` and
    `cash` values; each run starts from cash, so its first day's cash share is 1."""
    dates, returns, cash_before = [], [], []
    for run in runs:
        previous = Decimal(run["starting_cash"])
        share = Decimal(1)
        for day in run["equity_curve"]:
            equity = Decimal(day["equity"])
            dates.append(date.fromisoformat(day["date"]))
            returns.append(equity / previous - 1)
            cash_before.append(share)
            share, previous = Decimal(day["cash"]) / equity, equity
    return Curve(dates, returns, cash_before)


def daily_returns(bars: Sequence[EquityBar]) -> dict[date, Decimal]:
    """Close-to-close returns by date; the first bar has none."""
    return {bar.date: bar.close / previous.close - 1
            for previous, bar in zip(bars, bars[1:], strict=False)}


def cash_returns(curve: Curve, rates: Mapping[date, Decimal] | None) -> list[Decimal]:
    """The cash return on each day: 0%, or the given daily rates (SHY)."""
    if rates is None:
        return [Decimal(0)] * len(curve.dates)
    missing = [day for day in curve.dates if day not in rates]
    if missing:
        raise ValueError(f"No cash return for {len(missing)} day(s), first {missing[0]}")
    return [rates[day] for day in curve.dates]


def credited(curve: Curve, cash: Sequence[Decimal]) -> list[Decimal]:
    """Daily returns with idle cash earning the cash return."""
    return [value + share * rate
            for value, share, rate in zip(curve.returns, curve.cash_before, cash, strict=True)]


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / len(values)


def _std(values: Sequence[Decimal]) -> Decimal:
    mean = _mean(values)
    return (sum(((value - mean) ** 2 for value in values), Decimal(0))
            / (len(values) - 1)).sqrt()


def _q(value: Decimal | None, places: str) -> str | None:
    return None if value is None else str(value.quantize(Decimal(places)))


def statistics(returns: Sequence[Decimal], cash: Sequence[Decimal]) -> dict:
    """Return, risk, and risk-adjusted measures for daily returns; Sharpe and
    Sortino use returns above the cash return."""
    if len(returns) < 2:
        raise ValueError("Statistics need at least two daily returns")
    levels, growth = [], Decimal(1)
    for value in returns:
        growth *= 1 + value
        levels.append(growth)
    excess = [value - rate for value, rate in zip(returns, cash, strict=True)]
    deviation, excess_deviation = _std(returns), _std(excess)
    downside = _mean([min(value, Decimal(0)) ** 2 for value in excess]).sqrt()
    return {
        "total_return_pct": _q((growth - 1) * 100, "0.001"),
        "annualized_return_pct": _q((growth ** (TRADING_DAYS / len(returns)) - 1) * 100,
                                    "0.001"),
        "annualized_volatility_pct": _q(deviation * ROOT_TRADING_DAYS * 100, "0.001"),
        "sharpe": _q(None if excess_deviation == 0 else
                     _mean(excess) / excess_deviation * ROOT_TRADING_DAYS, "0.01"),
        "sortino": _q(None if downside == 0 else
                      _mean(excess) / downside * ROOT_TRADING_DAYS, "0.01"),
        "max_drawdown_pct": _q(max_drawdown(Decimal(1), levels) * 100, "0.001"),
    }


def volatility_ratio(process: Sequence[Decimal], benchmark: Sequence[Decimal]) -> Decimal:
    """k: the benchmark fraction whose volatility equals the process's."""
    return _std(process) / _std(benchmark)


def risk_matched(benchmark: Sequence[Decimal], cash: Sequence[Decimal],
                 k: Decimal) -> list[Decimal]:
    """The benchmark held at fraction k, the rest in cash, rebalanced daily."""
    return [k * value + (1 - k) * rate for value, rate in zip(benchmark, cash, strict=True)]


def compare(process: Curve, benchmarks: Mapping[str, Curve],
            cash_rates: Mapping[date, Decimal]) -> dict:
    """Every measure and answer for one process, under both cash treatments."""
    for name, curve in benchmarks.items():
        if curve.dates != process.dates:
            raise ValueError(f"{name} does not cover the same days as the process")
    ratios = {name: volatility_ratio(process.returns, curve.returns)
              for name, curve in benchmarks.items()}
    treatments: dict[str, dict] = {}
    answers: dict[str, dict] = {}
    for treatment, rates in (("cash_0pct", None), ("cash_shy", cash_rates)):
        cash = cash_returns(process, rates)
        own = credited(process, cash)
        table = {"process": statistics(own, cash)}
        answers[treatment] = {}
        for name, curve in benchmarks.items():
            held = credited(curve, cash)
            matched = risk_matched(held, cash, ratios[name])
            table[name] = statistics(held, cash)
            table[f"{name}_risk_matched"] = statistics(matched, cash)
            process_sharpe, benchmark_sharpe = table["process"]["sharpe"], table[name]["sharpe"]
            answers[treatment][name] = {
                "beats_risk_matched_return": (
                    Decimal(table["process"]["total_return_pct"])
                    > Decimal(table[f"{name}_risk_matched"]["total_return_pct"])),
                "higher_sharpe": (process_sharpe is not None and benchmark_sharpe is not None
                                  and Decimal(process_sharpe) > Decimal(benchmark_sharpe))}
        treatments[treatment] = table
    return {"first_date": process.dates[0].isoformat(),
            "last_date": process.dates[-1].isoformat(), "days": len(process.dates),
            "volatility_ratio_k": {name: _q(value, "0.0001") for name, value in ratios.items()},
            "measures": treatments, "answers": answers}


def _reproduced(summary: dict, record: dict, study: str) -> None:
    logged = record.get("results", {}).get("rolling")
    if summary != logged:
        raise NotReproduced(f"{study} did not reproduce its logged rolling summary "
                            f"(experiment {record.get('experiment_id')}); the review stops")


def review_portfolio_study(study: str, universe: Universe,
                           candidates: tuple[PortfolioCandidate, ...], record: dict,
                           cash_rates: Mapping[date, Decimal]) -> dict:
    """ADR-011 to ADR-013: the rolling windows before the holdout, at $0.01."""
    rows = universe_rows(universe)
    validation_end = len(universe.dates) * 4 // 5
    runs = rolling_runs(universe, rows, PortfolioConfig(), validation_end, candidates)
    _reproduced(rolling_summary(runs), record, study)
    return {"study": study, "experiment_id": record["experiment_id"], "reproduced": True,
            **compare(chain([run.strategy for run in runs]),
                      {"b1": chain([run.b1 for run in runs]),
                       "b2": chain([run.b2 for run in runs])}, cash_rates)}


def _buy_and_hold_run(bars: list[EquityBar]) -> dict:
    """ADR-010's 100% GLD benchmark over one test block: buy at the first open."""
    config = RESEARCH_CONFIG
    price = bars[0].open + config.slippage_per_share
    budget = config.starting_cash - config.commission_per_order
    shares = int((budget / price).to_integral_value(rounding=ROUND_DOWN))
    cash = config.starting_cash - shares * price - config.commission_per_order
    curve = [{"date": bar.date.isoformat(), "cash": str(cash),
              "equity": str(cash + shares * bar.close)} for bar in bars]
    total = (Decimal(curve[-1]["equity"]) / config.starting_cash - 1) * 100
    return {"starting_cash": str(config.starting_cash), "equity_curve": curve,
            "total_return_pct": str(total.quantize(Decimal("0.001")))}


def _compounded_pct(values: Sequence[str]) -> str:
    growth = Decimal(1)
    for value in values:
        growth *= 1 + Decimal(value) / 100
    return str(((growth - 1) * 100).quantize(Decimal("0.001")))


def review_gld_study(bars: list[EquityBar], record: dict,
                     cash_rates: Mapping[date, Decimal]) -> dict:
    """ADR-010: the rolling windows before the GLD holdout, rebuilt from the same
    selection functions with RESEARCH_CONFIG, against 100% GLD buy-and-hold."""
    config = RESEARCH_CONFIG
    periods = chronological_split(bars)
    pre = periods["development"] + periods["validation"]
    rows = history_rows(bars, GLD_CANDIDATES)
    span = ROLL_DEVELOPMENT_BARS + ROLL_VALIDATION_BARS + ROLL_TEST_BARS
    runs, holds, windows = [], [], []
    for start in range(0, len(pre) - span + 1, ROLL_TEST_BARS):
        validation_start = start + ROLL_DEVELOPMENT_BARS
        test_start = validation_start + ROLL_VALIDATION_BARS
        test = pre[test_start:start + span]
        choice = select_candidate(
            pre[start:validation_start], pre[validation_start:test_start], config,
            GLD_CANDIDATES, None if rows is None else rows[start:validation_start],
            None if rows is None else rows[validation_start:test_start])
        result = run_selected(test, config, choice,
                              None if rows is None else rows[test_start:start + span])
        hold = _buy_and_hold_run(test)
        runs.append({**result, "starting_cash": str(config.starting_cash)})
        holds.append(hold)
        windows.append((choice.selected is not None, result["total_return_pct"],
                        hold["total_return_pct"], len(result["closed_trades"])))
    summary = {
        "windows": len(windows),
        "selected_windows": sum(selected for selected, *_ in windows),
        "positive_windows": sum(Decimal(value) > 0 for _, value, _, _ in windows),
        # RESEARCH_CONFIG caps positions at 100%, so the same-cap benchmark is 100% GLD.
        "beat_buy_hold_50pct_windows": sum(Decimal(value) > Decimal(hold)
                                           for _, value, hold, _ in windows),
        "closed_trades": sum(trades for *_, trades in windows),
        "compounded_return_pct": _compounded_pct([value for _, value, _, _ in windows]),
        "compounded_buy_hold_100pct_return_pct": _compounded_pct(
            [hold for _, _, hold, _ in windows]),
    }
    _reproduced(summary, record, "ADR-010")
    return {"study": "ADR-010", "experiment_id": record["experiment_id"], "reproduced": True,
            **compare(chain(runs), {"gld": chain(holds)}, cash_rates)}


def build_report(studies: list[dict], cash_source: dict) -> dict:
    return {"schema_version": 1, "mode": "risk_review", "adr": "ADR-014",
            "information_only": True, "cash_proxy": cash_source, "studies": studies,
            "notes": [
                "Information only: every period here was seen before this review was "
                "designed, so it cannot count as evidence or unlock trading.",
                "Risk-matched benchmarks hold the benchmark at the process's volatility, "
                "rebalanced daily without costs.",
                "A process that wins on risk-adjusted terms earns more than the benchmark "
                "only with leverage, which carries the risk of large losses."]}


def render_markdown(report: dict) -> str:
    rows = ["# Risk-adjusted review (ADR-014, information only)", "",
            report["notes"][0], "",
            f"Cash proxy: {report['cash_proxy']['symbol']} "
            f"(`{report['cash_proxy']['sha256'][:16]}`).", ""]
    for study in report["studies"]:
        rows += [f"## {study['study']} (experiment `{study['experiment_id']}`)", "",
                 f"{study['days']} test days, {study['first_date']} to {study['last_date']}. "
                 "Reproduced its logged rolling summary exactly. Volatility ratio k: "
                 + ", ".join(f"{name} {value}" for name, value
                             in study["volatility_ratio_k"].items()) + ".", ""]
        for treatment, label in (("cash_0pct", "Cash at 0%"), ("cash_shy", "Cash earns SHY")):
            rows += [f"**{label}**", "",
                     "| Portfolio | Total return | Annualized | Volatility | Sharpe | "
                     "Sortino | Max drawdown |",
                     "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
            for name, item in study["measures"][treatment].items():
                rows.append(f"| {name} | {item['total_return_pct']}% | "
                            f"{item['annualized_return_pct']}% | "
                            f"{item['annualized_volatility_pct']}% | {item['sharpe'] or 'n/a'} | "
                            f"{item['sortino'] or 'n/a'} | {item['max_drawdown_pct']}% |")
            rows.append("")
            for name, answer in study["answers"][treatment].items():
                rows.append(f"- Against {name}: beats risk-matched return: "
                            f"**{'yes' if answer['beats_risk_matched_return'] else 'no'}**; "
                            f"higher Sharpe: "
                            f"**{'yes' if answer['higher_sharpe'] else 'no'}**")
            rows.append("")
    rows += ["## Notes", "", *(f"- {note}" for note in report["notes"]), ""]
    return "\n".join(rows)
