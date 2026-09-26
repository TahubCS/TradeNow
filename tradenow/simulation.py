"""A deliberately small, deterministic MGC research simulation."""

from dataclasses import dataclass
from decimal import Decimal

from .market_data import Bar


@dataclass(frozen=True)
class Config:
    starting_cash: Decimal = Decimal("100000")
    contract_multiplier: Decimal = Decimal("10")
    tick_size: Decimal = Decimal("0.10")
    commission_per_side: Decimal = Decimal("1.50")
    max_notional_fraction: Decimal = Decimal("0.50")
    max_drawdown_fraction: Decimal = Decimal("0.10")
    fast_window: int = 3
    slow_window: int = 5


def simulate(bars: list[Bar], config: Config = Config()) -> dict:
    if len(bars) < config.slow_window + 1:
        raise ValueError("Need at least slow_window + 1 bars for next-open execution")
    if not 0 < config.fast_window < config.slow_window:
        raise ValueError("Expected 0 < fast_window < slow_window")
    money_values = (config.starting_cash, config.contract_multiplier, config.tick_size,
                    config.commission_per_side, config.max_notional_fraction,
                    config.max_drawdown_fraction)
    if any(not value.is_finite() for value in money_values):
        raise ValueError("Simulation configuration must be finite")
    if (config.starting_cash <= 0 or config.contract_multiplier <= 0
            or config.tick_size < 0 or config.commission_per_side < 0
            or not 0 < config.max_notional_fraction <= 1
            or not 0 < config.max_drawdown_fraction <= 1):
        raise ValueError("Invalid simulation configuration")

    cash = config.starting_cash
    entry_price: Decimal | None = None
    entry_date: str | None = None
    target = 0
    halted = False
    peak_equity = cash
    max_drawdown = Decimal("0")
    signals: list[dict] = []
    decisions: list[dict] = []
    fills: list[dict] = []
    trades: list[dict] = []
    equity_curve: list[dict] = []

    for index, bar in enumerate(bars):
        # The target was computed at the previous close, so today's open can fill it.
        if target == 1 and entry_price is None:
            fill_price = bar.open + config.tick_size
            notional = fill_price * config.contract_multiplier
            approved = notional <= cash * config.max_notional_fraction
            decisions.append({
                "date": bar.date.isoformat(),
                "action": "BUY",
                "approved": approved,
                "reason": "WITHIN_NOTIONAL_LIMIT" if approved else "NOTIONAL_LIMIT",
            })
            if approved:
                entry_price = fill_price
                entry_date = bar.date.isoformat()
                cash -= config.commission_per_side
                fills.append({"date": bar.date.isoformat(), "side": "BUY",
                              "price": str(fill_price), "contracts": 1})
        elif target == 0 and entry_price is not None:
            fill_price = bar.open - config.tick_size
            gross_pnl = (fill_price - entry_price) * config.contract_multiplier
            net_pnl = gross_pnl - 2 * config.commission_per_side
            cash += gross_pnl - config.commission_per_side
            trades.append({"entry_date": entry_date, "exit_date": bar.date.isoformat(),
                           "gross_pnl": str(gross_pnl), "net_pnl": str(net_pnl)})
            entry_price = None
            entry_date = None
            decisions.append({"date": bar.date.isoformat(), "action": "SELL",
                              "approved": True, "reason": "CLOSE_POSITION"})
            fills.append({"date": bar.date.isoformat(), "side": "SELL",
                          "price": str(fill_price), "contracts": 1})

        equity = cash if entry_price is None else cash + (bar.close - entry_price) * config.contract_multiplier
        peak_equity = max(peak_equity, equity)
        drawdown = (peak_equity - equity) / peak_equity
        max_drawdown = max(max_drawdown, drawdown)
        equity_curve.append({"date": bar.date.isoformat(), "equity": str(equity)})
        if drawdown >= config.max_drawdown_fraction and not halted:
            halted = True
            decisions.append({"date": bar.date.isoformat(), "action": "HALT_NEW_ENTRIES",
                              "approved": False, "reason": "DRAWDOWN_LIMIT"})

        if index >= config.slow_window - 1:
            fast = sum(item.close for item in bars[index + 1 - config.fast_window:index + 1])
            slow = sum(item.close for item in bars[index + 1 - config.slow_window:index + 1])
            strategy_target = int(fast / config.fast_window > slow / config.slow_window)
            target = 0 if halted else strategy_target
            signals.append({"date": bar.date.isoformat(),
                            "strategy_target_contracts": strategy_target,
                            "target_contracts": target})

    total_pnl = equity - config.starting_cash
    return {
        "mode": "offline_simulation",
        "contract": bars[0].contract,
        "bars": len(bars),
        "starting_cash": str(config.starting_cash),
        "ending_equity": str(equity),
        "total_pnl": str(total_pnl),
        "total_return_pct": str((total_pnl / config.starting_cash * 100).quantize(Decimal("0.001"))),
        "max_drawdown_pct": str((max_drawdown * 100).quantize(Decimal("0.001"))),
        "open_contracts": int(entry_price is not None),
        "halted": halted,
        "signals": signals,
        "risk_decisions": decisions,
        "fills": fills,
        "closed_trades": trades,
        "equity_curve": equity_curve,
    }
