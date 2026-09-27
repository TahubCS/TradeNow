"""Multi-asset selection, holdout, and rolling tests (ADR-011).

The method is ADR-010's, applied to portfolios: each candidate runs on the
development and validation periods, the highest validation return minus
maximum drawdown wins (the earlier candidate on a tie), and a best score that
is not positive selects cash. The gate uses only the rolling windows before
the final holdout; the holdout is reported for information.

Features are computed once per symbol over the whole history. Each row uses
only earlier bars, so a window's lookbacks reach before its first bar without
seeing the future. Every run starts from cash (ADR-011 clarification 10).
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from .equity_types import FeatureRow
from .features import compute_features
from .gld_evaluation import (
    ROLL_DEVELOPMENT_BARS,
    ROLL_TEST_BARS,
    ROLL_VALIDATION_BARS,
    STRESSED_SLIPPAGE_PER_SHARE,
)
from .metrics import max_drawdown
from .multi_strategies import MULTI_CANDIDATES, MultiCandidate
from .portfolio import (
    Allocator,
    PortfolioConfig,
    benchmark_equal_weight,
    benchmark_spy,
    simulate_portfolio,
)
from .selection import split_points, validation_score
from .universe import Universe


Rows = dict[str, list[FeatureRow]]
ROLL_SPAN = ROLL_DEVELOPMENT_BARS + ROLL_VALIDATION_BARS + ROLL_TEST_BARS


def _pct(value: Decimal) -> str:
    return str((value * 100).quantize(Decimal("0.001")))


def universe_rows(universe: Universe) -> Rows:
    """Point-in-time features for every symbol, from its adjusted bars."""
    return {symbol: compute_features(universe.bars(symbol)) for symbol in universe.symbols}


def summarize(result: dict) -> dict:
    """The parts of a portfolio run that reports keep."""
    return {"first_date": result["first_date"], "last_date": result["last_date"],
            "total_return_pct": result["total_return_pct"],
            "max_drawdown_pct": result["max_drawdown_pct"],
            "ending_equity": result["ending_equity"],
            "closed_trades": len(result["closed_trades"]), "fills": len(result["fills"]),
            "unfilled_orders": len(result["unfilled_orders"]),
            "turnover_pct": result["turnover_pct"], "halted": result["halted"],
            "contribution_pct": result["contribution_pct"], "metrics": result["metrics"]}


@dataclass(frozen=True)
class MultiSelection:
    selected: MultiCandidate | None  # None: no positive validation score (cash)
    best: MultiCandidate
    best_score: Decimal
    hypotheses: list[dict]


def select_multi(universe: Universe, rows: Rows, config: PortfolioConfig,
                 development: tuple[int, int], validation: tuple[int, int],
                 candidates: tuple[MultiCandidate, ...] = MULTI_CANDIDATES) -> MultiSelection:
    """ADR-010's rule: highest validation return minus maximum drawdown, else cash."""
    if not candidates:
        raise ValueError("At least one candidate is required")
    hypotheses = []
    best: MultiCandidate | None = None
    best_score: Decimal | None = None
    for candidate in candidates:
        developed = simulate_portfolio(universe, candidate, config, rows, *development)
        validated = simulate_portfolio(universe, candidate, config, rows, *validation)
        score = validation_score(validated)
        hypotheses.append({"name": candidate.name, "parameters": candidate.parameters,
                           "development": summarize(developed),
                           "validation": summarize(validated),
                           "validation_score": str(score)})
        if best_score is None or score > best_score:
            best, best_score = candidate, score
    assert best is not None and best_score is not None
    return MultiSelection(best if best_score > 0 else None, best, best_score, hypotheses)


def run_selection(universe: Universe, rows: Rows, config: PortfolioConfig,
                  choice: MultiSelection, start: int, end: int) -> dict:
    """The selected candidate on new bars; with no selection the run holds cash."""
    allocator: Allocator = (choice.selected if choice.selected is not None
                            else lambda _views: {})
    return simulate_portfolio(universe, allocator, config, rows, start, end)


def chain_curves(results: Sequence[dict]) -> list[Decimal]:
    """Daily equity of consecutive runs joined end to end, starting at 1: each run
    begins where the previous one ended (ADR-011 clarification 11)."""
    level, chained = Decimal(1), []
    for result in results:
        start = Decimal(result["starting_cash"])
        values = [level * Decimal(day["equity"]) / start for day in result["equity_curve"]]
        chained.extend(values)
        level = values[-1]
    return chained


def _chained_drawdown(results: Sequence[dict]) -> str:
    return _pct(max_drawdown(Decimal(1), chain_curves(results))) if results else "0.000"


def _compounded(returns_pct: Sequence[str]) -> str:
    growth = Decimal(1)
    for value in returns_pct:
        growth *= 1 + Decimal(value) / 100
    return _pct(growth - 1)


def window_starts(bars: int) -> list[int]:
    """Development starts of the rolling windows inside the first `bars` bars;
    test blocks step by their own length, so they never overlap."""
    return list(range(0, bars - ROLL_SPAN + 1, ROLL_TEST_BARS))


def rolling_checks(universe: Universe, rows: Rows, config: PortfolioConfig, bars: int,
                   candidates: tuple[MultiCandidate, ...] = MULTI_CANDIDATES) -> dict:
    """Rolling 504/126/126 windows inside the first `bars` bars; every selection
    uses only bars before its test block."""
    windows, runs, b1_runs, b2_runs = [], [], [], []
    dates = universe.dates
    for start in window_starts(bars):
        validation_start = start + ROLL_DEVELOPMENT_BARS
        test_start = validation_start + ROLL_VALIDATION_BARS
        test_end = start + ROLL_SPAN
        choice = select_multi(universe, rows, config, (start, validation_start),
                              (validation_start, test_start), candidates)
        result = run_selection(universe, rows, config, choice, test_start, test_end)
        b1 = benchmark_equal_weight(universe, config, test_start, test_end)
        b2 = benchmark_spy(universe, config, test_start, test_end)
        runs.append(result)
        b1_runs.append(b1)
        b2_runs.append(b2)
        strategy = Decimal(result["total_return_pct"])
        windows.append({
            "development_start": dates[start].isoformat(),
            "validation_start": dates[validation_start].isoformat(),
            "test_start": dates[test_start].isoformat(),
            "test_end": dates[test_end - 1].isoformat(),
            "selected_hypothesis": None if choice.selected is None else choice.selected.name,
            "best_candidate": choice.best.name, "best_validation_score": str(choice.best_score),
            "test_return_pct": result["total_return_pct"],
            "test_max_drawdown_pct": result["max_drawdown_pct"],
            "b1_return_pct": b1["total_return_pct"], "b2_return_pct": b2["total_return_pct"],
            "closed_trades": len(result["closed_trades"]), "halted": result["halted"],
            "beat_b1": strategy > Decimal(b1["total_return_pct"]),
            "beat_b2": strategy > Decimal(b2["total_return_pct"])})
    return {
        "development_bars": ROLL_DEVELOPMENT_BARS, "validation_bars": ROLL_VALIDATION_BARS,
        "test_bars": ROLL_TEST_BARS, "step_bars": ROLL_TEST_BARS,
        "slippage_per_share": str(config.slippage_per_share),
        "windows": windows,
        "summary": {
            "windows": len(windows),
            "selected_windows": sum(item["selected_hypothesis"] is not None
                                    for item in windows),
            "positive_windows": sum(Decimal(item["test_return_pct"]) > 0 for item in windows),
            "beat_b1_windows": sum(item["beat_b1"] for item in windows),
            "beat_b2_windows": sum(item["beat_b2"] for item in windows),
            "beat_both_windows": sum(item["beat_b1"] and item["beat_b2"] for item in windows),
            "closed_trades": sum(item["closed_trades"] for item in windows),
            "compounded_return_pct": _compounded([item["test_return_pct"] for item in windows]),
            "compounded_b1_return_pct": _compounded([item["b1_return_pct"] for item in windows]),
            "compounded_b2_return_pct": _compounded([item["b2_return_pct"] for item in windows]),
            "chained_max_drawdown_pct": _chained_drawdown(runs),
            "chained_b1_max_drawdown_pct": _chained_drawdown(b1_runs),
            "chained_b2_max_drawdown_pct": _chained_drawdown(b2_runs)}}


def evaluate_multi(universe: Universe, config: PortfolioConfig = PortfolioConfig(),
                   candidates: tuple[MultiCandidate, ...] = MULTI_CANDIDATES,
                   rows: Rows | None = None) -> dict:
    """Selection on the 60/20 periods, the holdout (information only), and the
    rolling checks before the holdout at normal and stressed slippage."""
    rows = universe_rows(universe) if rows is None else rows
    count = len(universe.dates)
    development_end, validation_end = split_points(count)
    if validation_end < ROLL_SPAN:
        raise ValueError(f"Need at least {ROLL_SPAN} bars before the holdout for one "
                         f"rolling window; have {validation_end}")
    choice = select_multi(universe, rows, config, (0, development_end),
                          (development_end, validation_end), candidates)
    holdout = run_selection(universe, rows, config, choice, validation_end, count)
    stressed = replace(config, slippage_per_share=STRESSED_SLIPPAGE_PER_SHARE)
    dates = universe.dates
    return {
        "periods": {name: {"first_date": dates[start].isoformat(),
                           "last_date": dates[end - 1].isoformat(), "bars": end - start}
                    for name, start, end in (("development", 0, development_end),
                                             ("validation", development_end, validation_end),
                                             ("holdout", validation_end, count))},
        "selection": {"selected_hypothesis": (None if choice.selected is None
                                              else choice.selected.name),
                      "best_candidate": choice.best.name,
                      "best_validation_score": str(choice.best_score),
                      "hypotheses": choice.hypotheses},
        "holdout": {"strategy": summarize(holdout),
                    "b1": summarize(benchmark_equal_weight(universe, config,
                                                           validation_end, count)),
                    "b2": summarize(benchmark_spy(universe, config, validation_end, count))},
        "rolling_pre_holdout": rolling_checks(universe, rows, config, validation_end,
                                              candidates),
        "rolling_pre_holdout_stressed": rolling_checks(universe, rows, stressed,
                                                       validation_end, candidates),
    }
