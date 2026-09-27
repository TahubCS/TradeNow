"""Pure paper-trading rules: signal, data cross-check, reconciliation, drawdown, sizing.

Nothing here performs I/O, so every rule can be tested without Alpaca.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from .alpaca_paper import FINAL_ORDER_STATUSES, BrokerOrder, DailyBar, Position
from .equity import EquityBar


# A buy may fill up to 1% above the last close. A larger upward gap leaves the
# order unfilled, where the backtest would have bought at any open.
BUY_LIMIT_BUFFER = Decimal("0.01")
CLOSE_TOLERANCE = Decimal("0.005")
MIN_OVERLAP_SESSIONS = 5
VOLUME_LOOKBACK_SESSIONS = 20
# Local-only statuses: written before the POST, and for an order Alpaca never received.
SUBMITTING = "submitting"
NOT_FOUND = "not_found"
LEDGER_FINAL_STATUSES = FINAL_ORDER_STATUSES | {NOT_FOUND}


@dataclass(frozen=True)
class Signal:
    fast_sma: Decimal
    slow_sma: Decimal
    strategy_target: int


@dataclass(frozen=True)
class LedgerOrder:
    """This system's own record of an order it sent (or tried to send)."""
    client_order_id: str
    plan_id: str
    session: date
    side: str
    qty: int
    limit_price: Decimal | None
    status: str
    filled_qty: int = 0
    filled_avg_price: Decimal | None = None
    broker_order_id: str | None = None
    # Execution-quality evidence: the plan's price, when this system sent the
    # order, and Alpaca's own submission and fill times.
    reference_close: Decimal | None = None
    sent_at: datetime | None = None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    # Who approved sending it: a person ("manual") or paper-auto ("auto").
    approval: str = "manual"

    @property
    def is_final(self) -> bool:
        return self.status in LEDGER_FINAL_STATUSES

    @property
    def signed_fill(self) -> int:
        return self.filled_qty if self.side == "buy" else -self.filled_qty


@dataclass(frozen=True)
class Reconciliation:
    expected_shares: int
    broker_shares: int
    open_orders: int
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class Drawdown:
    peak_equity: Decimal
    equity: Decimal
    fraction: Decimal
    breached: bool


@dataclass(frozen=True)
class DailyLoss:
    last_equity: Decimal | None
    equity: Decimal
    fraction: Decimal | None
    breached: bool


@dataclass(frozen=True)
class PlannedAction:
    action: str  # BUY, SELL, NO_TRADE, or REJECTED
    reason: str
    qty: int = 0
    limit_price: Decimal | None = None


def sma_signal(bars: list[EquityBar], fast: int, slow: int) -> Signal:
    """The same SMA rule as simulate_equity, evaluated at the last close."""
    if not 0 < fast < slow or len(bars) < slow:
        raise ValueError("Not enough GLD bars for the selected SMA windows")
    fast_sma = sum((bar.close for bar in bars[-fast:]), Decimal(0)) / fast
    slow_sma = sum((bar.close for bar in bars[-slow:]), Decimal(0)) / slow
    return Signal(fast_sma, slow_sma, int(fast_sma > slow_sma))


def cross_check_closes(tiingo: list[EquityBar], alpaca: list[DailyBar]) -> list[str]:
    """Alpaca bars verify Tiingo; they never feed the signal."""
    if not tiingo or not alpaca:
        return ["NO_BARS_TO_COMPARE"]
    problems = []
    tiingo_last, alpaca_last = tiingo[-1].date, alpaca[-1].date
    if alpaca_last > tiingo_last:
        problems.append(f"TIINGO_STALE: Alpaca has the {alpaca_last} session; "
                        f"the Tiingo import ends {tiingo_last}")
    elif tiingo_last > alpaca_last:
        problems.append(f"ALPACA_MISSING_SESSION: Tiingo has {tiingo_last}; "
                        f"Alpaca ends {alpaca_last}")
    tiingo_closes = {bar.date: bar.close for bar in tiingo if bar.date >= alpaca[0].date}
    alpaca_closes = {bar.date: bar.close for bar in alpaca if bar.date <= tiingo_last}
    differing = sorted(set(tiingo_closes) ^ set(alpaca_closes))
    if differing:
        problems.append("SESSION_DATES_DIFFER: " +
                        ", ".join(day.isoformat() for day in differing[:5]))
    shared = sorted(set(tiingo_closes) & set(alpaca_closes))
    if len(shared) < MIN_OVERLAP_SESSIONS:
        problems.append(f"TOO_FEW_SHARED_SESSIONS: {len(shared)}")
    for day in shared:
        expected, actual = tiingo_closes[day], alpaca_closes[day]
        if abs(actual - expected) / expected > CLOSE_TOLERANCE:
            problems.append(f"CLOSE_MISMATCH {day}: Tiingo {expected}, Alpaca {actual}")
    return problems


def apply_broker_order(order: LedgerOrder, broker: BrokerOrder | None,
                       definitive: bool) -> LedgerOrder:
    """Update a ledger record from Alpaca. A missing order becomes NOT_FOUND only
    when the absence is definitive; otherwise it stays unresolved and blocks trading."""
    if broker is None:
        return replace(order, status=NOT_FOUND) if definitive else order
    if (broker.symbol != "GLD" or broker.side != order.side or broker.qty != order.qty
            or broker.client_order_id != order.client_order_id):
        raise ValueError(f"Alpaca order {order.client_order_id} differs from the ledger")
    return replace(order, status=broker.status, filled_qty=broker.filled_qty,
                   filled_avg_price=broker.filled_avg_price,
                   broker_order_id=broker.broker_order_id,
                   submitted_at=broker.submitted_at or order.submitted_at,
                   filled_at=broker.filled_at or order.filled_at)


def reconcile(ledger: list[LedgerOrder], positions: list[Position],
              open_orders: list[BrokerOrder]) -> Reconciliation:
    """Broker state is the source of truth; any difference blocks new orders."""
    problems = []
    expected = sum(order.signed_fill for order in ledger)
    broker_shares = 0
    for position in positions:
        if position.symbol == "GLD":
            broker_shares = position.shares
        else:
            problems.append(f"UNEXPECTED_POSITION: {position.symbol}")
    if expected < 0:
        problems.append(f"LEDGER_NEGATIVE_POSITION: {expected}")
    if expected != broker_shares:
        problems.append(f"POSITION_MISMATCH: ledger {expected} shares, "
                        f"Alpaca {broker_shares} shares")
    known = {order.client_order_id for order in ledger}
    for order in open_orders:
        if order.client_order_id not in known:
            problems.append(f"UNKNOWN_OPEN_ORDER: {order.symbol} {order.side} {order.qty}")
    open_ids = {order.client_order_id for order in open_orders}
    pending = [order for order in ledger if not order.is_final]
    for record in pending:
        if record.status == SUBMITTING:
            problems.append(f"UNRESOLVED_SUBMISSION: {record.client_order_id}")
        elif record.client_order_id not in open_ids:
            problems.append(f"LEDGER_ORDER_NOT_OPEN: {record.client_order_id}")
    if len(pending) > 1:
        problems.append(f"DUPLICATE_OPEN_ORDERS: {len(pending)}")
    return Reconciliation(expected, broker_shares, len(open_orders), tuple(problems))


def measure_drawdown(peak: Decimal | None, equity: Decimal,
                     threshold: Decimal) -> Drawdown:
    """Same rule as the simulator: closing equity against the highest seen."""
    if not equity.is_finite() or equity <= 0:
        raise ValueError("Paper account equity must be positive")
    new_peak = equity if peak is None else max(peak, equity)
    fraction = (new_peak - equity) / new_peak
    return Drawdown(new_peak, equity, fraction, fraction >= threshold)


def measure_daily_loss(last_equity: Decimal | None, equity: Decimal,
                       limit: Decimal) -> DailyLoss:
    """Loss since the previous close. An unknown baseline counts as breached (fail closed)."""
    if last_equity is None or not last_equity.is_finite() or last_equity <= 0:
        return DailyLoss(last_equity, equity, None, True)
    fraction = max(Decimal(0), (last_equity - equity) / last_equity)
    return DailyLoss(last_equity, equity, fraction, fraction >= limit)


def volume_cap(bars: list[EquityBar], fraction: Decimal) -> int:
    """Largest buy allowed: a fraction of the recent average daily volume."""
    recent = bars[-VOLUME_LOOKBACK_SESSIONS:]
    if not recent:
        return 0
    average = Decimal(sum(bar.volume for bar in recent)) / len(recent)
    return int((average * fraction).to_integral_value(rounding=ROUND_DOWN))


def plan_order(target: int, shares: int, cash: Decimal, reference_close: Decimal,
               max_position_fraction: Decimal, commission: Decimal,
               buy_limit_buffer: Decimal = BUY_LIMIT_BUFFER,
               max_buy_shares: int | None = None) -> PlannedAction:
    """Size a buy from cash like the simulator; never use margin buying power.
    Sells are never reduced: an exit must always be possible."""
    if target and not shares:
        limit = (reference_close * (1 + buy_limit_buffer)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
        budget = cash * max_position_fraction - commission
        qty = max(0, int((budget / limit).to_integral_value(rounding=ROUND_DOWN)))
        if qty == 0:
            return PlannedAction("REJECTED", "INSUFFICIENT_CASH"
                                 if cash < limit + commission else "POSITION_LIMIT")
        if max_buy_shares is not None and qty > max_buy_shares:
            if max_buy_shares <= 0:
                return PlannedAction("REJECTED", "VOLUME_LIMIT")
            return PlannedAction("BUY", "VOLUME_LIMIT", max_buy_shares, limit)
        return PlannedAction("BUY", "STRATEGY_SIGNAL", qty, limit)
    if not target and shares:
        return PlannedAction("SELL", "CLOSE_POSITION", shares)
    return PlannedAction("NO_TRADE", "AT_TARGET")
