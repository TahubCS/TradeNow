"""A deliberately small, deterministic MGC research simulation."""

from dataclasses import dataclass
from decimal import Decimal

from .execution import ApprovedOrder, SimulatedBroker
from .market_data import Bar


@dataclass(frozen=True)
class Config:
    starting_cash: Decimal = Decimal("100000")
    contract_multiplier: Decimal = Decimal("10")
    tick_size: Decimal = Decimal("0.10")
    commission_per_side: Decimal = Decimal("1.50")
    max_notional_fraction: Decimal = Decimal("0.50")
    max_drawdown_fraction: Decimal = Decimal("0.10")
    initial_margin_fraction: Decimal = Decimal("0.10")
    maintenance_margin_fraction: Decimal = Decimal("0.08")
    max_order_gap_days: int = 7
    expiry_exit_days: int = 5
    fast_window: int = 3
    slow_window: int = 5
    enable_entries: bool = True


def _regular_session_open(bar: Bar) -> bool:
    if bar.open_time_ct is None:
        return True  # Daily bars without a timestamp cannot verify trading hours.
    weekday = bar.open_time_ct.weekday()
    minute = bar.open_time_ct.hour * 60 + bar.open_time_ct.minute
    if weekday == 6:
        return minute >= 17 * 60
    if weekday in range(4):
        return minute < 16 * 60 or minute >= 17 * 60
    return weekday == 4 and minute < 16 * 60


def _unfilled_reason(bars: list[Bar], index: int, config: Config) -> str | None:
    bar = bars[index]
    if bar.volume == 0:
        return "ZERO_VOLUME"
    if not _regular_session_open(bar):
        return "SESSION_CLOSED"
    if index and (bar.date - bars[index - 1].date).days > config.max_order_gap_days:
        return "STALE_BAR_GAP"
    return None


def _opening_gap_pct(bars: list[Bar], index: int) -> str | None:
    if not index or bars[index - 1].contract != bars[index].contract:
        return None
    gap = (bars[index].open / bars[index - 1].close - 1) * 100
    return str(gap.quantize(Decimal("0.001")))


def simulate(bars: list[Bar], config: Config = Config()) -> dict:
    if len(bars) < config.slow_window + 1:
        raise ValueError("Need at least slow_window + 1 bars for next-open execution")
    if not 0 < config.fast_window < config.slow_window:
        raise ValueError("Expected 0 < fast_window < slow_window")
    money_values = (config.starting_cash, config.contract_multiplier, config.tick_size,
                    config.commission_per_side, config.max_notional_fraction,
                    config.max_drawdown_fraction, config.initial_margin_fraction,
                    config.maintenance_margin_fraction)
    if any(not value.is_finite() for value in money_values):
        raise ValueError("Simulation configuration must be finite")
    if (config.starting_cash <= 0 or config.contract_multiplier <= 0
            or config.tick_size < 0 or config.commission_per_side < 0
            or not 0 < config.max_notional_fraction <= 10
            or not 0 < config.max_drawdown_fraction <= 1
            or not 0 < config.maintenance_margin_fraction <= config.initial_margin_fraction <= 1
            or config.max_order_gap_days < 1 or config.expiry_exit_days < 1):
        raise ValueError("Invalid simulation configuration")

    broker = SimulatedBroker(config.starting_cash, config.contract_multiplier,
                             config.tick_size, config.commission_per_side)
    target = 0
    halted = False
    peak_equity = broker.cash
    max_drawdown = Decimal("0")
    signals: list[dict] = []
    proposals: list[dict] = []
    decisions: list[dict] = []
    unfilled_orders: list[dict] = []
    rolls: list[dict] = []
    equity_curve: list[dict] = []
    segment_start = 0

    for index, bar in enumerate(bars):
        if index and bar.contract != bars[index - 1].contract:
            segment_start = index
            if broker.entry_price is not None:
                raise RuntimeError("Open contract crossed a roll without an exit")
        next_bar = bars[index + 1] if index + 1 < len(bars) else None
        roll_to = (next_bar.contract if next_bar and next_bar.contract != bar.contract
                   else None)
        expiry_guard = bool(bar.last_trade_date and
                            (bar.last_trade_date - bar.date).days <= config.expiry_exit_days)

        # The target was computed at the previous close, so today's open can fill it.
        action = ("BUY" if target == 1 and broker.entry_price is None else
                  "SELL" if target == 0 and broker.entry_price is not None else None)
        blocked = None
        if action == "BUY" and roll_to:
            blocked = "ROLL_PENDING"
        elif action == "BUY" and expiry_guard:
            blocked = "EXPIRY_GUARD"
        elif action:
            blocked = _unfilled_reason(bars, index, config)
        if action and blocked:
            decisions.append({
                "date": bar.date.isoformat(), "action": action,
                "approved": False, "reason": blocked,
            })
            unfilled_orders.append({"date": bar.date.isoformat(), "side": action,
                                    "contract": bar.contract, "reason": blocked})
        elif action == "BUY":
            fill_price = bar.open + config.tick_size
            notional = fill_price * config.contract_multiplier
            approved = notional <= broker.cash * config.max_notional_fraction
            margin_ok = (broker.cash - config.commission_per_side >=
                         notional * config.initial_margin_fraction)
            reason = ("NOTIONAL_LIMIT" if not approved else
                      "INITIAL_MARGIN" if not margin_ok else "WITHIN_LIMITS")
            decisions.append({"date": bar.date.isoformat(), "action": "BUY",
                              "approved": approved and margin_ok, "reason": reason})
            if approved and margin_ok:
                fill = broker.submit(ApprovedOrder(f"{bar.date.isoformat()}-BUY",
                                                    bar.date.isoformat(), "BUY", bar.open,
                                                    bar.contract))
                fill["opening_gap_pct"] = _opening_gap_pct(bars, index)
        elif action == "SELL":
            decisions.append({"date": bar.date.isoformat(), "action": "SELL",
                              "approved": True, "reason": "CLOSE_POSITION"})
            fill = broker.submit(ApprovedOrder(f"{bar.date.isoformat()}-SELL",
                                                bar.date.isoformat(), "SELL", bar.open,
                                                bar.contract))
            fill["opening_gap_pct"] = _opening_gap_pct(bars, index)

        if roll_to or expiry_guard:
            if broker.entry_price is not None:
                if _unfilled_reason(bars, index, config):
                    raise ValueError(f"Cannot close {bar.contract} on an untradable roll/expiry bar")
                reason = "ROLL_EXIT" if roll_to else "EXPIRY_EXIT"
                decisions.append({"date": bar.date.isoformat(), "action": "SELL",
                                  "approved": True, "reason": reason})
                broker.submit(ApprovedOrder(f"{bar.date.isoformat()}-{reason}",
                                            bar.date.isoformat(), "SELL", bar.close,
                                            bar.contract, "CLOSE"))
            if roll_to:
                rolls.append({"date": bar.date.isoformat(), "from_contract": bar.contract,
                              "to_contract": roll_to})

        broker.reconcile()
        equity = broker.equity(bar.close)
        peak_equity = max(peak_equity, equity)
        drawdown = (peak_equity - equity) / peak_equity
        max_drawdown = max(max_drawdown, drawdown)
        equity_curve.append({"date": bar.date.isoformat(), "equity": str(equity),
                             "position_contracts": int(broker.entry_price is not None)})
        if drawdown >= config.max_drawdown_fraction and not halted:
            halted = True
            decisions.append({"date": bar.date.isoformat(), "action": "HALT_NEW_ENTRIES",
                              "approved": False, "reason": "DRAWDOWN_LIMIT"})
        if (broker.entry_price is not None and not halted and
                equity < bar.close * config.contract_multiplier *
                config.maintenance_margin_fraction):
            halted = True
            decisions.append({"date": bar.date.isoformat(), "action": "HALT_NEW_ENTRIES",
                              "approved": False, "reason": "MAINTENANCE_MARGIN"})

        if index - segment_start >= config.slow_window - 1:
            fast = sum((item.close for item in bars[index + 1 - config.fast_window:index + 1]),
                       Decimal(0))
            slow = sum((item.close for item in bars[index + 1 - config.slow_window:index + 1]),
                       Decimal(0))
            fast_average = fast / config.fast_window
            slow_average = slow / config.slow_window
            strategy_target = int(fast_average > slow_average)
            target = (0 if halted or not config.enable_entries or roll_to or expiry_guard
                      else strategy_target)
            signals.append({"date": bar.date.isoformat(),
                            "strategy_target_contracts": strategy_target,
                            "target_contracts": target})
            action = ("BUY" if target == 1 and broker.entry_price is None else
                      "SELL" if target == 0 and broker.entry_price is not None else "NO_TRADE")
            proposals.append({"date": bar.date.isoformat(), "action": action,
                              "requested_contracts": target,
                              "evidence": {"fast_sma": str(fast_average.quantize(Decimal("0.001"))),
                                           "slow_sma": str(slow_average.quantize(Decimal("0.001")))},
                              "reason": ("DRAWDOWN_OR_MARGIN_HALT" if halted else
                                         "ROLL_BOUNDARY" if roll_to else
                                         "EXPIRY_GUARD" if expiry_guard else
                                         "SELECTION_GATE" if not config.enable_entries else
                                         "STRATEGY_SIGNAL")})
        else:
            target = 0

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
        "open_contracts": int(broker.entry_price is not None),
        "open_contract": broker.entry_contract,
        "halted": halted,
        "signals": signals,
        "proposals": proposals,
        "risk_decisions": decisions,
        "fills": broker.fills,
        "unfilled_orders": unfilled_orders,
        "rolls": rolls,
        "closed_trades": broker.closed_trades,
        "equity_curve": equity_curve,
    }
