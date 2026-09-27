"""Portfolio performance metrics (Phase 4), computed from a daily equity curve.

Conventions, applied to every strategy and benchmark alike:

- Daily returns are close-to-close equity changes; the first is measured from
  the starting equity. 252 trading days make a year.
- The risk-free rate is zero, matching the zero-interest cash benchmark.
- Sortino's downside deviation averages squared negative returns over all days.
- Holding periods are calendar days from entry fill to exit fill.

Nothing here performs I/O, and all arithmetic uses Decimal.
"""

from datetime import date
from decimal import Decimal


TRADING_DAYS = Decimal(252)
ROOT_TRADING_DAYS = TRADING_DAYS.sqrt()


def _q(value: Decimal | None, places: str = "0.001") -> str | None:
    return None if value is None else str(value.quantize(Decimal(places)))


def daily_returns(start: Decimal, equity: list[Decimal]) -> list[Decimal]:
    previous, returns = start, []
    for value in equity:
        returns.append(value / previous - 1)
        previous = value
    return returns


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / len(values)


def _std(values: list[Decimal]) -> Decimal:
    """Sample standard deviation."""
    mean = _mean(values)
    return (sum(((value - mean) ** 2 for value in values), Decimal(0))
            / (len(values) - 1)).sqrt()


def max_drawdown(start: Decimal, equity: list[Decimal]) -> Decimal:
    peak, worst = start, Decimal(0)
    for value in equity:
        peak = max(peak, value)
        worst = max(worst, (peak - value) / peak)
    return worst


def performance(start: Decimal, equity: list[Decimal], first_date: date, last_date: date,
                trades: list[dict] | None = None,
                invested_days: int | None = None) -> dict:
    """Return, risk, and trade statistics for one equity curve."""
    if start <= 0 or not equity or any(value <= 0 for value in equity):
        raise ValueError("Performance needs positive starting and daily equity")
    returns = daily_returns(start, equity)
    total = equity[-1] / start - 1
    years = Decimal((last_date - first_date).days) / Decimal("365.25")
    annualized = ((equity[-1] / start) ** (1 / years) - 1) if years > 0 else None
    volatility = sharpe = sortino = None
    if len(returns) >= 2:
        deviation = _std(returns)
        volatility = deviation * ROOT_TRADING_DAYS
        mean = _mean(returns)
        if deviation > 0:
            sharpe = mean / deviation * ROOT_TRADING_DAYS
        downside = (_mean([min(value, Decimal(0)) ** 2 for value in returns])).sqrt()
        if downside > 0:
            sortino = mean / downside * ROOT_TRADING_DAYS
    drawdown = max_drawdown(start, equity)
    calmar = (annualized / drawdown if annualized is not None and drawdown > 0 else None)
    result = {
        "total_return_pct": _q(total * 100),
        "annualized_return_pct": _q(None if annualized is None else annualized * 100),
        "annualized_volatility_pct": _q(None if volatility is None else volatility * 100),
        "sharpe": _q(sharpe, "0.01"), "sortino": _q(sortino, "0.01"),
        "max_drawdown_pct": _q(drawdown * 100), "calmar": _q(calmar, "0.01"),
        "exposure_pct": (None if invested_days is None
                         else _q(Decimal(invested_days) * 100 / len(equity))),
    }
    if trades is not None:
        result.update(trade_statistics(trades))
    return result


def trade_statistics(trades: list[dict]) -> dict:
    """Closed round trips: count, hit rate, profit factor, and holding period."""
    pnl = [Decimal(trade["net_pnl"]) for trade in trades]
    wins = sum((value for value in pnl if value > 0), Decimal(0))
    losses = -sum((value for value in pnl if value < 0), Decimal(0))
    held = [(date.fromisoformat(str(trade["exit_date"]))
             - date.fromisoformat(str(trade["entry_date"]))).days for trade in trades]
    return {
        "closed_trades": len(trades),
        "hit_rate_pct": (None if not trades
                         else _q(Decimal(sum(value > 0 for value in pnl)) * 100 / len(pnl))),
        "profit_factor": (None if not trades or losses == 0 else _q(wins / losses, "0.01")),
        "average_holding_days": (None if not held
                                 else _q(Decimal(sum(held)) / len(held), "0.1")),
    }


def equity_performance(result: dict, starting_cash: Decimal) -> dict:
    """Metrics for a simulate_equity result."""
    curve = result["equity_curve"]
    return performance(starting_cash, [Decimal(day["equity"]) for day in curve],
                       date.fromisoformat(str(curve[0]["date"])),
                       date.fromisoformat(str(curve[-1]["date"])),
                       result["closed_trades"],
                       sum(day["position_shares"] > 0 for day in curve))
