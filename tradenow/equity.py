"""Deterministic, cash-funded GLD share simulation using local daily bars."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from .equity_types import EquityBar, FeatureRow
from .features import compute_features
from .strategies import History, SmaCross, Strategy


@dataclass(frozen=True)
class EquityConfig:
    starting_cash: Decimal = Decimal("100000")
    max_position_fraction: Decimal = Decimal("0.50")
    max_drawdown_fraction: Decimal = Decimal("0.10")
    slippage_per_share: Decimal = Decimal("0.01")
    commission_per_order: Decimal = Decimal("0")
    max_order_gap_days: int = 7
    fast_window: int = 3
    slow_window: int = 10
    enable_entries: bool = True


def validate_equity_bars(bars: list[EquityBar], config: EquityConfig) -> None:
    if not 0 < config.fast_window < config.slow_window:
        raise ValueError("Expected 0 < fast_window < slow_window")
    if len(bars) < config.slow_window + 1:
        raise ValueError("Need at least slow_window + 1 bars for next-open execution")
    values = (config.starting_cash, config.max_position_fraction,
              config.max_drawdown_fraction, config.slippage_per_share,
              config.commission_per_order)
    if any(not value.is_finite() for value in values):
        raise ValueError("Equity configuration must be finite")
    if (config.starting_cash <= 0 or not 0 < config.max_position_fraction <= 1
            or not 0 < config.max_drawdown_fraction <= 1
            or config.slippage_per_share < 0 or config.commission_per_order < 0
            or config.max_order_gap_days < 1):
        raise ValueError("Invalid equity configuration")
    for index, bar in enumerate(bars):
        if bar.symbol != "GLD":
            raise ValueError("Equity simulation accepts GLD only")
        if bar.date.weekday() >= 5 or (index and bar.date <= bars[index - 1].date):
            raise ValueError("GLD bar dates must be increasing weekdays")
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not price.is_finite() or price <= 0 for price in prices):
            raise ValueError("GLD prices must be finite and positive")
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            raise ValueError("Invalid GLD OHLC range")
        if bar.volume < 0:
            raise ValueError("GLD volume cannot be negative")


def plain_target(target: Decimal) -> int | str:
    """Whole targets stay integers in reports, as before fractional sizing existed."""
    return int(target) if target == target.to_integral_value() else str(target)


def feature_rows(bars: list[EquityBar], strategy: Strategy,
                 features: Sequence[FeatureRow] | None = None) -> Sequence[FeatureRow]:
    """The rows a strategy reads: given, computed, or closes only if it needs no features."""
    if features is not None:
        if (len(features) != len(bars)
                or any(row.date != bar.date for row, bar in zip(features, bars, strict=True))):
            raise ValueError("Feature rows do not match the GLD bars")
        return features
    if strategy.needs_features:
        return compute_features(bars)
    return [FeatureRow(bar.date, bar.close, {}) for bar in bars]


def simulate_equity(bars: list[EquityBar], config: EquityConfig = EquityConfig(),
                    strategy: Strategy | None = None,
                    features: Sequence[FeatureRow] | None = None) -> dict:
    """Compute signals at each close and fill approved whole-share orders next open.

    Without a strategy, the SMA crossover set by config's windows is used.
    """
    validate_equity_bars(bars, config)
    strategy = strategy or SmaCross(config.fast_window, config.slow_window)
    rows = feature_rows(bars, strategy, features)
    cash = config.starting_cash
    shares = 0
    entry_price: Decimal | None = None
    entry_date: str | None = None
    target = Decimal(0)
    halted = False
    drawdown_halt: dict | None = None
    peak_equity = cash
    max_drawdown = Decimal(0)
    signals: list[dict] = []
    proposals: list[dict] = []
    risk_decisions: list[dict] = []
    fills: list[dict] = []
    closed_trades: list[dict] = []
    unfilled_orders: list[dict] = []
    equity_curve: list[dict] = []

    for index, bar in enumerate(bars):
        day = bar.date.isoformat()
        action = "BUY" if target and not shares else "SELL" if not target and shares else None
        if action:
            blocked = ("ZERO_VOLUME" if bar.volume == 0 else
                       "STALE_BAR_GAP" if index and
                       (bar.date - bars[index - 1].date).days > config.max_order_gap_days
                       else None)
            if blocked:
                risk_decisions.append({"date": day, "action": action,
                                       "approved": False, "reason": blocked})
                unfilled_orders.append({"date": day, "side": action,
                                        "symbol": "GLD", "reason": blocked})
            elif action == "BUY":
                price = bar.open + config.slippage_per_share
                budget = (cash * config.max_position_fraction * target
                          - config.commission_per_order)
                quantity = max(0, int((budget / price).to_integral_value(rounding=ROUND_DOWN)))
                if quantity == 0:
                    reason = ("INSUFFICIENT_CASH" if cash < price + config.commission_per_order
                              else "POSITION_LIMIT")
                    risk_decisions.append({"date": day, "action": action,
                                           "approved": False, "reason": reason})
                else:
                    cost = price * quantity + config.commission_per_order
                    if cost > cash:
                        raise RuntimeError("Approved GLD purchase exceeds cash")
                    cash -= cost
                    shares = quantity
                    entry_price = price
                    entry_date = day
                    risk_decisions.append({"date": day, "action": action,
                                           "approved": True, "reason": "WITHIN_LIMITS",
                                           "shares": quantity})
                    fills.append({"order_id": f"{day}-BUY", "date": day,
                                  "side": action, "symbol": "GLD",
                                  "price": str(price), "shares": quantity})
            else:
                price = bar.open - config.slippage_per_share
                if price <= 0:
                    raise ValueError("Simulated GLD sale price must be positive")
                assert entry_price is not None  # a sell only follows a filled buy
                quantity = shares
                cash += price * quantity - config.commission_per_order
                gross_pnl = (price - entry_price) * quantity
                net_pnl = gross_pnl - 2 * config.commission_per_order
                closed_trades.append({"entry_date": entry_date, "exit_date": day,
                                      "symbol": "GLD", "shares": quantity,
                                      "gross_pnl": str(gross_pnl), "net_pnl": str(net_pnl)})
                shares = 0
                entry_price = None
                entry_date = None
                risk_decisions.append({"date": day, "action": action,
                                       "approved": True,
                                       "reason": ("DRAWDOWN_EXIT" if halted else "CLOSE_POSITION"),
                                       "shares": quantity})
                fills.append({"order_id": f"{day}-SELL", "date": day,
                              "side": action, "symbol": "GLD",
                              "price": str(price), "shares": quantity})
                if halted:
                    assert drawdown_halt is not None
                    drawdown_halt["exit_date"] = day
                    drawdown_halt["exit_price"] = str(price)

        if sum(fill["shares"] * (1 if fill["side"] == "BUY" else -1)
               for fill in fills) != shares:
            raise RuntimeError("GLD position does not match fill ledger")
        equity = cash + shares * bar.close
        peak_equity = max(peak_equity, equity)
        current_drawdown = (peak_equity - equity) / peak_equity
        max_drawdown = max(max_drawdown, current_drawdown)
        equity_curve.append({"date": day, "cash": str(cash), "equity": str(equity),
                             "position_shares": shares})
        if current_drawdown >= config.max_drawdown_fraction and not halted:
            halted = True
            drawdown_halt = {
                "threshold_pct": str(config.max_drawdown_fraction * 100),
                "trigger_date": day,
                "trigger_drawdown_pct": str((current_drawdown * 100).quantize(Decimal("0.001"))),
                "shares_at_trigger": shares,
                "exit_date": None,
                "exit_price": None,
            }
            risk_decisions.append({"date": day, "action": "HALT_NEW_ENTRIES",
                                   "approved": False, "reason": "DRAWDOWN_LIMIT"})

        decision = strategy.decide(History(rows, index), holding=shares > 0)
        if decision is not None:
            strategy_target = decision.target
            target = strategy_target if config.enable_entries and not halted else Decimal(0)
            signals.append({"date": day, "strategy_target": plain_target(strategy_target),
                            "target": plain_target(target), **decision.evidence})
            proposed_action = ("BUY" if target and not shares else
                               "SELL" if not target and shares else "NO_TRADE")
            proposals.append({"date": day, "action": proposed_action,
                              "target_position": plain_target(target),
                              "reason": ("DRAWDOWN_HALT" if halted else
                                         "SELECTION_GATE" if not config.enable_entries else
                                         "STRATEGY_SIGNAL")})

    total_pnl = equity - config.starting_cash
    if drawdown_halt is not None:
        drawdown_halt["exit_pending"] = shares > 0
    return {"mode": "offline_equity_simulation", "symbol": "GLD", "bars": len(bars),
            "starting_cash": str(config.starting_cash), "ending_cash": str(cash),
            "ending_equity": str(equity), "total_pnl": str(total_pnl),
            "total_return_pct": str((total_pnl / config.starting_cash * 100)
                                    .quantize(Decimal("0.001"))),
            "max_drawdown_pct": str((max_drawdown * 100).quantize(Decimal("0.001"))),
            "open_shares": shares, "halted": halted,
            "drawdown_halt": drawdown_halt, "signals": signals,
            "proposals": proposals,
            "risk_decisions": risk_decisions, "fills": fills,
            "unfilled_orders": unfilled_orders, "closed_trades": closed_trades,
            "equity_curve": equity_curve}
