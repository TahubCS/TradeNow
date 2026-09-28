"""Pure rules for fixed-mix mode (ADR-015): no files, no network, Decimal only.

- plan_rebalance turns targets, holdings, closes, and cash into orders:
  sells first (an evening of their own), then buys that fit the cash.
- rebalance_due decides when a quarter's check runs.
- drawdown_alerts reports drawdown levels once each, per peak.
- reconcile_mix compares this mode's ledger with Alpaca, symbol by symbol.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from .alpaca_paper import BrokerOrder, Position
from .paper_rules import LEDGER_FINAL_STATUSES, NOT_FOUND, SUBMITTING


BAND = Decimal("0.05")  # an asset trades only beyond 5% of equity from its target
DRAWDOWN_ALERT_LEVELS = (Decimal("0.10"), Decimal("0.20"), Decimal("0.30"))
CENT = Decimal("0.01")


def quarter(day: date) -> str:
    return f"{day.year}Q{(day.month - 1) // 3 + 1}"


def rebalance_due(checked_quarter: str | None, checked_mix: str | None, rebalancing: bool,
                  mix_sha256: str, latest_close: date) -> bool:
    """On the first run, in a new quarter, after a mix change, or while a
    quarter's rebalance is unfinished."""
    return (rebalancing or checked_quarter != quarter(latest_close)
            or checked_mix != mix_sha256)


@dataclass(frozen=True)
class MixAction:
    symbol: str
    side: str  # "buy" or "sell"
    qty: int
    limit_price: Decimal | None  # buys only; sells are market orders
    reason: str


@dataclass(frozen=True)
class RebalancePlan:
    phase: str  # "SELL", "BUY", or "NONE"
    actions: tuple[MixAction, ...]
    equity: Decimal
    weights: dict[str, Decimal]  # current share of equity per symbol
    drift: dict[str, Decimal]  # current weight minus target
    note: str


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_DOWN))


def plan_rebalance(targets: Mapping[str, Decimal], shares: Mapping[str, int],
                   closes: Mapping[str, Decimal], cash: Decimal,
                   buy_limit_buffer: Decimal, band: Decimal = BAND) -> RebalancePlan:
    """The orders that bring every asset outside its band back to target.

    A symbol held but no longer in the mix has target 0 and is sold whatever
    its size. Buys are limit orders at the close plus the buffer, and their
    total at the limits never exceeds cash; when it would, every buy shrinks
    by the same fraction and rounds down."""
    if cash < 0:
        raise ValueError("Negative cash: margin is in use")
    held = [symbol for symbol, count in shares.items() if count]
    symbols = list(targets) + [symbol for symbol in held if symbol not in targets]
    missing = [symbol for symbol in symbols if symbol not in closes]
    if missing:
        raise ValueError(f"No close for {', '.join(missing)}")
    if any(closes[symbol] <= 0 for symbol in symbols):
        raise ValueError("Closes must be positive")
    value = {symbol: shares.get(symbol, 0) * closes[symbol] for symbol in symbols}
    equity = cash + sum(value.values(), Decimal(0))
    if equity <= 0:
        raise ValueError("Equity must be positive")
    target = {symbol: targets.get(symbol, Decimal(0)) for symbol in symbols}
    weights = {symbol: value[symbol] / equity for symbol in symbols}
    drift = {symbol: weights[symbol] - target[symbol] for symbol in symbols}
    sells = []
    for symbol in symbols:
        count = shares.get(symbol, 0)
        if count and (symbol not in targets or drift[symbol] > band):
            keep = _floor(target[symbol] * equity / closes[symbol])
            if count > keep:
                sells.append(MixAction(symbol, "sell", count - keep, None,
                                       "NOT_IN_MIX" if symbol not in targets else "ABOVE_BAND"))
    if sells:
        return RebalancePlan("SELL", tuple(sells), equity, weights, drift,
                             "sells first; buys are planned on a later evening")
    below = [symbol for symbol in targets if -drift[symbol] > band]
    wanted = []
    for symbol in below:
        limit = (closes[symbol] * (1 + buy_limit_buffer)).quantize(CENT, ROUND_HALF_UP)
        qty = _floor(target[symbol] * equity / limit) - shares.get(symbol, 0)
        if qty > 0:
            wanted.append((symbol, qty, limit))
    if not below:
        return RebalancePlan("NONE", (), equity, weights, drift, "every asset is within its band")
    if not wanted:
        return RebalancePlan("NONE", (), equity, weights, drift,
                             "the remaining buys cannot afford a single share")
    cost = sum((qty * limit for _, qty, limit in wanted), Decimal(0))
    buys = []
    for symbol, qty, limit in wanted:
        # floor(qty x cash / cost) keeps the total at the limits within cash.
        sized = qty if cost <= cash else int((qty * cash) // cost)
        if sized:
            buys.append(MixAction(symbol, "buy", sized, limit,
                                  "BELOW_BAND" if sized == qty else "SHRUNK_TO_CASH"))
    if not buys:
        return RebalancePlan("NONE", (), equity, weights, drift,
                             "the remaining buys cannot afford a single share")
    return RebalancePlan("BUY", tuple(buys), equity, weights, drift, "buys within cash")


@dataclass(frozen=True)
class DrawdownAlerts:
    peak: Decimal
    alerted: Decimal  # the highest level already alerted since this peak
    fraction: Decimal
    crossed: tuple[Decimal, ...]  # levels to alert now


def drawdown_alerts(peak: Decimal | None, alerted: Decimal, equity: Decimal,
                    levels: Sequence[Decimal] = DRAWDOWN_ALERT_LEVELS) -> DrawdownAlerts:
    """Each level is alerted once per peak; a new peak starts over."""
    if not equity.is_finite() or equity <= 0:
        raise ValueError("Account equity must be positive")
    if peak is None or equity > peak:
        peak, alerted = equity, Decimal(0)
    fraction = (peak - equity) / peak
    crossed = tuple(level for level in levels if alerted < level <= fraction)
    return DrawdownAlerts(peak, max((alerted, *crossed)), fraction, crossed)


@dataclass(frozen=True)
class MixOrderRecord:
    """This mode's own record of an order it sent (or tried to send)."""
    client_order_id: str
    plan_id: str
    session: date
    symbol: str
    side: str
    qty: int
    limit_price: Decimal | None
    status: str
    reference_close: Decimal
    filled_qty: int = 0
    filled_avg_price: Decimal | None = None
    broker_order_id: str | None = None
    sent_at: datetime | None = None
    approval: str = "auto"

    @property
    def is_final(self) -> bool:
        return self.status in LEDGER_FINAL_STATUSES

    @property
    def signed_fill(self) -> int:
        return self.filled_qty if self.side == "buy" else -self.filled_qty


def apply_broker(record: MixOrderRecord, broker: BrokerOrder | None,
                 definitive: bool) -> MixOrderRecord:
    """Update a record from Alpaca. A missing order becomes NOT_FOUND only when
    the absence is definitive; otherwise it stays unresolved and blocks trading."""
    if broker is None:
        return replace(record, status=NOT_FOUND) if definitive else record
    if (broker.symbol != record.symbol or broker.side != record.side
            or broker.qty != record.qty or broker.client_order_id != record.client_order_id):
        raise ValueError(f"Alpaca order {record.client_order_id} differs from the mix ledger")
    return replace(record, status=broker.status, filled_qty=broker.filled_qty,
                   filled_avg_price=broker.filled_avg_price,
                   broker_order_id=broker.broker_order_id)


@dataclass(frozen=True)
class MixReconciliation:
    expected: dict[str, int]
    broker: dict[str, int]
    open_orders: int
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


def reconcile_mix(ledger: Sequence[MixOrderRecord], positions: Sequence[Position],
                  open_orders: Sequence[BrokerOrder]) -> MixReconciliation:
    """Alpaca is the source of truth; any difference blocks trading."""
    expected: dict[str, int] = {}
    for record in ledger:
        expected[record.symbol] = expected.get(record.symbol, 0) + record.signed_fill
    broker = {position.symbol: position.shares for position in positions}
    problems = []
    for symbol in sorted(set(expected) | set(broker)):
        ours, theirs = expected.get(symbol, 0), broker.get(symbol, 0)
        if ours < 0:
            problems.append(f"LEDGER_NEGATIVE_POSITION: {symbol} {ours}")
        if symbol not in expected:
            problems.append(f"UNEXPECTED_POSITION: {symbol} {theirs} shares")
        elif ours != theirs:
            problems.append(f"POSITION_MISMATCH: {symbol} ledger {ours}, Alpaca {theirs}")
    known = {record.client_order_id for record in ledger}
    for order in open_orders:
        if order.client_order_id not in known:
            problems.append(f"UNKNOWN_OPEN_ORDER: {order.symbol} {order.side} {order.qty}")
    open_ids = {order.client_order_id for order in open_orders}
    for record in ledger:
        if record.is_final:
            continue
        if record.status == SUBMITTING:
            problems.append(f"UNRESOLVED_SUBMISSION: {record.client_order_id}")
        elif record.client_order_id not in open_ids:
            problems.append(f"LEDGER_ORDER_NOT_OPEN: {record.client_order_id}")
    return MixReconciliation({symbol: count for symbol, count in expected.items() if count},
                             {symbol: count for symbol, count in broker.items() if count},
                             len(open_orders), tuple(problems))
