"""Offline GLD benchmarks, cost sensitivity, and pre-holdout rolling checks."""

from dataclasses import replace
from decimal import ROUND_DOWN, Decimal

from .equity import EquityBar, EquityConfig, simulate_equity
from .offline import CANDIDATES


ROLL_DEVELOPMENT_BARS = 504
ROLL_VALIDATION_BARS = 126
ROLL_TEST_BARS = 126
# ADR-008 R4: the rolling result must survive ten times the default slippage.
STRESSED_SLIPPAGE_PER_SHARE = Decimal("0.10")


def _pct(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.001")))


def _annualized_pct(start_equity: Decimal, end_equity: Decimal, calendar_days: int) -> str:
    if calendar_days <= 0 or end_equity <= 0:
        raise ValueError("Annualized return needs positive equity and elapsed time")
    years = Decimal(calendar_days) / Decimal("365.25")
    return _pct(((end_equity / start_equity) ** (1 / years) - 1) * 100)


def _buy_and_hold(bars: list[EquityBar], config: EquityConfig,
                  fraction: Decimal) -> dict:
    """Buy at the first test open, hold through the last close, without rebalancing."""
    if not bars or bars[0].volume <= 0:
        raise ValueError("Buy-and-hold needs a tradable first test bar")
    price = bars[0].open + config.slippage_per_share
    budget = config.starting_cash * fraction - config.commission_per_order
    shares = int((budget / price).to_integral_value(rounding=ROUND_DOWN))
    if shares < 1:
        raise ValueError("Buy-and-hold cannot afford one GLD share")
    cash = config.starting_cash - shares * price - config.commission_per_order
    peak = config.starting_cash
    max_drawdown = Decimal(0)
    for bar in bars:
        equity = cash + shares * bar.close
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
    total_return = (equity / config.starting_cash - 1) * 100
    return {"shares": shares, "ending_equity": str(equity),
            "total_return_pct": _pct(total_return),
            "annualized_return_pct": _annualized_pct(config.starting_cash, equity,
                                                       (bars[-1].date - bars[0].date).days),
            "max_drawdown_pct": _pct(max_drawdown * 100)}


def _turnover_pct(result: dict, starting_cash: Decimal) -> str:
    traded = sum(Decimal(fill["price"]) * fill["shares"] for fill in result["fills"])
    return _pct(traded / starting_cash * 100)


def _compounded(returns_pct) -> str:
    """Chain window returns as if each test began with the previous window's equity."""
    growth = Decimal(1)
    for value in returns_pct:
        growth *= 1 + Decimal(value) / 100
    return _pct((growth - 1) * 100)


def _rolling_checks(bars: list[EquityBar], config: EquityConfig) -> dict:
    """Disjoint test blocks; every selection uses only earlier bars."""
    span = ROLL_DEVELOPMENT_BARS + ROLL_VALIDATION_BARS + ROLL_TEST_BARS
    windows = []
    for start in range(0, len(bars) - span + 1, ROLL_TEST_BARS):
        development = bars[start:start + ROLL_DEVELOPMENT_BARS]
        validation = bars[start + ROLL_DEVELOPMENT_BARS:
                          start + ROLL_DEVELOPMENT_BARS + ROLL_VALIDATION_BARS]
        test = bars[start + ROLL_DEVELOPMENT_BARS + ROLL_VALIDATION_BARS:start + span]
        best_score: Decimal | None = None
        best_name = None
        best_config = None
        best_development_return = None
        for name, fast, slow in CANDIDATES:
            candidate = replace(config, fast_window=fast, slow_window=slow)
            development_result = simulate_equity(development, candidate)
            measured = simulate_equity(validation, candidate)
            score = (Decimal(measured["total_return_pct"])
                     - Decimal(measured["max_drawdown_pct"]))
            if best_score is None or score > best_score:
                best_score, best_name, best_config = score, name, candidate
                best_development_return = development_result["total_return_pct"]
        assert best_score is not None and best_config is not None
        selected = best_name if best_score > 0 else None
        result = simulate_equity(test, replace(best_config, enable_entries=selected is not None))
        benchmark = _buy_and_hold(test, config, config.max_position_fraction)
        full_benchmark = _buy_and_hold(test, config, Decimal(1))
        strategy_return = Decimal(result["total_return_pct"])
        benchmark_return = Decimal(benchmark["total_return_pct"])
        windows.append({"development_start": development[0].date.isoformat(),
                        "validation_end": validation[-1].date.isoformat(),
                        "test_start": test[0].date.isoformat(),
                        "test_end": test[-1].date.isoformat(),
                        "selected_hypothesis": selected,
                        "best_candidate_development_return_pct": best_development_return,
                        "strategy_warmup_bars": len(test) - len(result["signals"]),
                        "test_return_pct": result["total_return_pct"],
                        "buy_hold_50pct_return_pct": benchmark["total_return_pct"],
                        "buy_hold_100pct_return_pct": full_benchmark["total_return_pct"],
                        "closed_trades": len(result["closed_trades"]),
                        "beat_benchmark": strategy_return > benchmark_return})
    return {"development_bars": ROLL_DEVELOPMENT_BARS,
            "validation_bars": ROLL_VALIDATION_BARS,
            "test_bars": ROLL_TEST_BARS, "step_bars": ROLL_TEST_BARS,
            "windows": windows,
            "summary": {"windows": len(windows),
                        "selected_windows": sum(item["selected_hypothesis"] is not None
                                                for item in windows),
                        "positive_windows": sum(Decimal(item["test_return_pct"]) > 0
                                                for item in windows),
                        "beat_buy_hold_50pct_windows": sum(item["beat_benchmark"]
                                                           for item in windows),
                        "closed_trades": sum(item["closed_trades"] for item in windows),
                        "compounded_return_pct": _compounded(
                            item["test_return_pct"] for item in windows),
                        "compounded_buy_hold_100pct_return_pct": _compounded(
                            item["buy_hold_100pct_return_pct"] for item in windows)}}


def evaluate_gld(pre_holdout: list[EquityBar], holdout: list[EquityBar],
                 config: EquityConfig, selected: str | None, holdout_result: dict) -> dict:
    """Evaluate a fixed selection; never choose a candidate using holdout bars."""
    days = (holdout[-1].date - holdout[0].date).days
    strategy_equity = Decimal(holdout_result["ending_equity"])
    benchmark_50 = _buy_and_hold(holdout, config, config.max_position_fraction)
    benchmark_100 = _buy_and_hold(holdout, config, Decimal(1))
    chosen = next((item for item in CANDIDATES if item[0] == selected), None)
    if selected is not None and chosen is None:
        raise ValueError("Selected GLD hypothesis is unknown")
    scenarios = []
    for extra in (Decimal(0), Decimal("0.04"), Decimal("0.09")):
        slip = config.slippage_per_share + extra
        scenario = replace(config, slippage_per_share=slip,
                           fast_window=chosen[1] if chosen else config.fast_window,
                           slow_window=chosen[2] if chosen else config.slow_window,
                           enable_entries=selected is not None)
        result = (holdout_result if extra == 0 else simulate_equity(holdout, scenario))
        scenarios.append({"slippage_per_share": str(slip),
                          "selected_hypothesis": selected,
                          "total_return_pct": result["total_return_pct"],
                          "ending_equity": result["ending_equity"],
                          "fills": len(result["fills"])})
    return {"holdout": {
                "strategy": {"total_return_pct": holdout_result["total_return_pct"],
                             "annualized_return_pct": _annualized_pct(
                                 config.starting_cash, strategy_equity, days),
                             "max_drawdown_pct": holdout_result["max_drawdown_pct"],
                             "gross_traded_notional_pct_of_starting_cash": _turnover_pct(
                                 holdout_result, config.starting_cash)},
                "buy_hold_50pct": benchmark_50,
                "buy_hold_100pct": benchmark_100,
                "cash": {"total_return_pct": "0.000", "annualized_return_pct": "0.000",
                         "max_drawdown_pct": "0.000"},
                "calendar_days": days},
            "slippage_sensitivity": scenarios,
            "rolling_pre_holdout": _rolling_checks(pre_holdout, config),
            "rolling_pre_holdout_stressed": {
                "slippage_per_share": str(STRESSED_SLIPPAGE_PER_SHARE),
                "summary": _rolling_checks(pre_holdout, replace(
                    config, slippage_per_share=STRESSED_SLIPPAGE_PER_SHARE))["summary"]},
            "notes": ["Buy-and-hold buys at the first holdout open and marks at the last close.",
                      "50% benchmark matches the strategy position cap; 100% is full exposure.",
                      "Cash assumes zero interest. Benchmarks do not use the strategy drawdown halt.",
                      "Annualized returns use calendar days and a 365.25-day year.",
                      "Turnover is gross traded notional divided by starting cash.",
                      "Rolling tests end before the final holdout and restart flat in each window.",
                      "Rolling strategies warm up inside each test; buy-and-hold enters at its first open."]}
