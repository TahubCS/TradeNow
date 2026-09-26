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
    fast_window: int = 3
    slow_window: int = 5


def simulate(bars: list[Bar], config: Config = Config()) -> dict:
    if len(bars) < config.slow_window + 1:
        raise ValueError("Need at least slow_window + 1 bars for next-open execution")
    if not 0 < config.fast_window < config.slow_window:
        raise ValueError("Expected 0 < fast_window < slow_window")
    if (config.starting_cash <= 0 or config.contract_multiplier <= 0
            or config.tick_size < 0 or config.commission_per_side < 0
            or not 0 < config.max_notional_fraction <= 1):
        raise ValueError("Invalid simulation configuration")

    cash = config.starting_cash
    entry_price: Decimal | None = None
    target = 0
    signals: list[dict] = []
    decisions: list[dict] = []
    fills: list[dict] = []

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
                cash -= config.commission_per_side
                fills.append({"date": bar.date.isoformat(), "side": "BUY",
                              "price": str(fill_price), "contracts": 1})
        elif target == 0 and entry_price is not None:
            fill_price = bar.open - config.tick_size
            cash += ((fill_price - entry_price) * config.contract_multiplier
                     - config.commission_per_side)
            entry_price = None
            decisions.append({"date": bar.date.isoformat(), "action": "SELL",
                              "approved": True, "reason": "CLOSE_POSITION"})
            fills.append({"date": bar.date.isoformat(), "side": "SELL",
                          "price": str(fill_price), "contracts": 1})

        if index >= config.slow_window - 1:
            fast = sum(item.close for item in bars[index + 1 - config.fast_window:index + 1])
            slow = sum(item.close for item in bars[index + 1 - config.slow_window:index + 1])
            target = int(fast / config.fast_window > slow / config.slow_window)
            signals.append({"date": bar.date.isoformat(), "target_contracts": target})

    last_close = bars[-1].close
    equity = cash if entry_price is None else cash + (last_close - entry_price) * config.contract_multiplier
    return {
        "mode": "offline_simulation",
        "contract": bars[0].contract,
        "bars": len(bars),
        "starting_cash": str(config.starting_cash),
        "ending_equity": str(equity),
        "open_contracts": int(entry_price is not None),
        "signals": signals,
        "risk_decisions": decisions,
        "fills": fills,
    }

