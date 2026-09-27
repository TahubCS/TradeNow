"""Deterministic multi-asset portfolio simulation (ADR-011, MA3).

Cash plus up to six ETF positions in whole shares, never margin. An
allocator sets target weights at each signal day's close (the first bar and
every month-end); orders fill at the next open. The rules follow ADR-011 and
its clarifications:

- Equity is valued at the fill-day open. A buy targets
  floor(weight × equity ÷ (open + slippage)) shares, and an asset trades only
  if its target value differs from its holding by more than the band (1% of
  equity). A target of 0 always sells the whole position.
- Sells fill before buys. If the buys cost more than the cash left, every buy
  shrinks by the same fraction and rounds down, so cash never goes negative.
- A zero-volume day or a stale gap blocks that asset's order; it retries at
  each next open, re-evaluated from scratch, until it fills or a newer signal
  replaces it.
- The drawdown halt is checked at each close: everything is sold from the
  next open and no new entries follow for the rest of the run.
- Every run starts from cash, and slippage is the only cost (zero commission).

Nothing here performs I/O, and all arithmetic uses Decimal.
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_DOWN, Decimal

from .equity_types import EquityBar, FeatureRow
from .metrics import performance
from .strategies import History
from .universe import Universe


# Target weights after a signal day's close, from history known at that close.
Allocator = Callable[[Mapping[str, History]], Mapping[str, Decimal]]


@dataclass(frozen=True)
class PortfolioConfig:
    starting_cash: Decimal = Decimal("100000")
    slippage_per_share: Decimal = Decimal("0.01")
    max_drawdown_fraction: Decimal = Decimal("0.10")
    rebalance_band: Decimal = Decimal("0.01")
    max_order_gap_days: int = 7
    drawdown_halt: bool = True  # the benchmarks run without it (ADR-011)


def _pct(value: Decimal) -> str:
    return str((value * 100).quantize(Decimal("0.001")))


def signal_days(dates: Sequence[date], start: int = 0, end: int | None = None) -> list[int]:
    """The first bar and every month-end in [start, end), except the last bar,
    which has no next open to fill at. A month-end is the last day of its
    calendar month in the whole aligned calendar."""
    end = len(dates) if end is None else end
    return [index for index in range(start, end - 1)
            if index == start or (dates[index].year, dates[index].month)
            != (dates[index + 1].year, dates[index + 1].month)]


def _validate(universe: Universe, config: PortfolioConfig, start: int, end: int,
              rows: Mapping[str, Sequence[FeatureRow]]) -> None:
    values = (config.starting_cash, config.slippage_per_share,
              config.max_drawdown_fraction, config.rebalance_band)
    if any(not value.is_finite() for value in values):
        raise ValueError("Portfolio configuration must be finite")
    if (config.starting_cash <= 0 or config.slippage_per_share < 0
            or not 0 < config.max_drawdown_fraction <= 1
            or not 0 <= config.rebalance_band < 1 or config.max_order_gap_days < 1):
        raise ValueError("Invalid portfolio configuration")
    symbols = universe.symbols
    if not symbols or len(set(symbols)) != len(symbols) or set(symbols) != set(universe.assets):
        raise ValueError("Universe symbols must be distinct and match its assets")
    if not 0 <= start < end - 1 < len(universe.dates):
        raise ValueError("A portfolio run needs at least two bars inside the calendar")
    # Evaluations simulate the same data hundreds of times; its bars and rows
    # are checked once. Holding the objects keeps the identity check sound.
    if _VALIDATED and _VALIDATED[0] is universe and _VALIDATED[1] is rows:
        return
    for symbol in symbols:
        bars = universe.bars(symbol)
        if [bar.date for bar in bars] != universe.dates:
            raise ValueError(f"{symbol} bars do not match the universe calendar")
        for bar in bars:
            if (bar.symbol != symbol or bar.volume < 0
                    or any(not price.is_finite() or price <= 0
                           for price in (bar.open, bar.close))):
                raise ValueError(f"Invalid {symbol} bar on {bar.date}")
        if [row.date for row in rows[symbol]] != universe.dates:
            raise ValueError(f"{symbol} feature rows do not match the universe calendar")
    _VALIDATED[:] = [universe, rows]


# The last universe and rows whose bars passed validation (see _validate).
_VALIDATED: list[object] = []


def _checked_weights(weights: Mapping[str, Decimal],
                     symbols: tuple[str, ...]) -> dict[str, Decimal]:
    """Allocator output is untrusted: known symbols, 0 <= weight, total <= 1."""
    unknown = sorted(set(weights) - set(symbols))
    if unknown:
        raise ValueError(f"Target weights name symbols outside the universe: {unknown}")
    checked = {}
    for symbol in symbols:
        weight = weights.get(symbol, Decimal(0))
        if not isinstance(weight, Decimal) or not weight.is_finite() or weight < 0:
            raise ValueError(f"Target weight for {symbol} must be a finite Decimal >= 0")
        checked[symbol] = weight
    if sum(checked.values(), Decimal(0)) > 1:
        raise ValueError("Target weights add up to more than 100%")
    return checked


@dataclass
class _Book:
    """Cash, positions, and the fill ledger, with round-trip bookkeeping."""
    cash: Decimal
    symbols: tuple[str, ...]
    shares: dict[str, int] = field(default_factory=dict)
    flow: dict[str, Decimal] = field(default_factory=dict)  # net cash per asset
    trips: dict[str, dict] = field(default_factory=dict)  # open round trips
    fills: list[dict] = field(default_factory=list)
    closed_trades: list[dict] = field(default_factory=list)
    traded: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        self.shares = dict.fromkeys(self.symbols, 0)
        self.flow = dict.fromkeys(self.symbols, Decimal(0))

    def fill(self, day: str, symbol: str, side: str, price: Decimal, quantity: int) -> None:
        amount = price * quantity
        if side == "BUY":
            if amount > self.cash:
                raise RuntimeError("Approved purchase exceeds cash")
            if self.shares[symbol] == 0:
                self.trips[symbol] = {"entry_date": day, "flow": Decimal(0)}
            self.cash -= amount
            self.shares[symbol] += quantity
            signed = -amount
        else:
            if price <= 0:
                raise ValueError("Simulated sale price must be positive")
            if quantity > self.shares[symbol]:
                raise RuntimeError("Sale exceeds the position")
            self.cash += amount
            self.shares[symbol] -= quantity
            signed = amount
        self.flow[symbol] += signed
        self.trips[symbol]["flow"] += signed
        self.traded += amount
        self.fills.append({"order_id": f"{day}-{symbol}-{side}", "date": day, "side": side,
                           "symbol": symbol, "price": str(price), "shares": quantity})
        if self.shares[symbol] == 0:
            trip = self.trips.pop(symbol)
            self.closed_trades.append({"symbol": symbol, "entry_date": trip["entry_date"],
                                       "exit_date": day, "gross_pnl": str(trip["flow"]),
                                       "net_pnl": str(trip["flow"])})


def _work_orders(book: _Book, bars: Mapping[str, EquityBar], gap_days: int,
                 targets: Mapping[str, Decimal], pending: set[str],
                 config: PortfolioConfig, decisions: list[dict],
                 unfilled: list[dict]) -> None:
    """One open: evaluate every pending asset, sell, then buy within the cash."""
    day = next(iter(bars.values())).date.isoformat()
    slippage = config.slippage_per_share
    equity = book.cash + sum((book.shares[s] * bars[s].open for s in book.symbols), Decimal(0))
    sells: dict[str, int] = {}
    buys: dict[str, int] = {}
    for symbol in book.symbols:
        if symbol not in pending:
            continue
        bar, held, weight = bars[symbol], book.shares[symbol], targets[symbol]
        if weight == 0 and held == 0:
            pending.discard(symbol)
            continue
        blocked = ("ZERO_VOLUME" if bar.volume == 0 else
                   "STALE_BAR_GAP" if gap_days > config.max_order_gap_days else None)
        if blocked:
            decisions.append({"date": day, "symbol": symbol, "action": "RETRY",
                              "reason": blocked})
            unfilled.append({"date": day, "symbol": symbol, "reason": blocked})
            continue
        pending.discard(symbol)
        if weight == 0:
            sells[symbol] = held
            continue
        target_value = weight * equity
        if abs(target_value - held * bar.open) <= config.rebalance_band * equity:
            decisions.append({"date": day, "symbol": symbol, "action": "NO_TRADE",
                              "reason": "WITHIN_BAND"})
            continue
        wanted = int((target_value / (bar.open + slippage)).to_integral_value(ROUND_DOWN))
        if wanted < held:
            sells[symbol] = held - wanted
        elif wanted > held:
            buys[symbol] = wanted - held
    for symbol, quantity in sells.items():
        book.fill(day, symbol, "SELL", bars[symbol].open - slippage, quantity)
        decisions.append({"date": day, "symbol": symbol, "action": "SELL",
                          "reason": "FULL_EXIT" if targets[symbol] == 0 else "REBALANCE",
                          "shares": quantity})
    cost = sum((quantity * (bars[s].open + slippage) for s, quantity in buys.items()),
               Decimal(0))
    available = book.cash  # after the sells, before any buy
    for symbol, wanted in buys.items():
        # Pro-rata shrink: floor(wanted × cash ÷ cost) keeps the total within cash.
        quantity = wanted if cost <= available else int((wanted * available) // cost)
        reason = "REBALANCE" if quantity == wanted else "SHRUNK_TO_CASH"
        if quantity:
            book.fill(day, symbol, "BUY", bars[symbol].open + slippage, quantity)
        decisions.append({"date": day, "symbol": symbol, "action": "BUY",
                          "reason": reason, "shares": quantity})


def simulate_portfolio(universe: Universe, allocator: Allocator,
                       config: PortfolioConfig = PortfolioConfig(),
                       rows: Mapping[str, Sequence[FeatureRow]] | None = None,
                       start: int = 0, end: int | None = None,
                       schedule: Collection[int] | None = None) -> dict:
    """Run the bars in [start, end) of the universe from cash.

    `rows` are each symbol's point-in-time features over the whole calendar
    (closes only if omitted); the allocator sees them through History views
    ending at the signal day. `schedule` overrides the signal days (B2 uses
    only the first bar).
    """
    end = len(universe.dates) if end is None else end
    symbols = universe.symbols
    if rows is None:
        rows = {s: [FeatureRow(bar.date, bar.close, {}) for bar in universe.bars(s)]
                for s in symbols}
    _validate(universe, config, start, end, rows)
    signals_at = set(signal_days(universe.dates, start, end) if schedule is None else schedule)
    if not signals_at <= set(range(start, end - 1)):
        raise ValueError("Signal days must fall before the last bar of the run")

    book = _Book(config.starting_cash, symbols)
    targets = dict.fromkeys(symbols, Decimal(0))
    pending: set[str] = set()
    halted = False
    drawdown_halt: dict | None = None
    peak = config.starting_cash
    worst = Decimal(0)
    signals: list[dict] = []
    decisions: list[dict] = []
    unfilled: list[dict] = []
    equity_curve: list[dict] = []
    invested_days = 0

    for index in range(start, end):
        bars = {s: universe.bars(s)[index] for s in symbols}
        day = universe.dates[index].isoformat()
        if pending:
            gap = (universe.dates[index] - universe.dates[index - 1]).days
            _work_orders(book, bars, gap, targets, pending, config, decisions, unfilled)
            if halted and drawdown_halt is not None and drawdown_halt["exit_date"] is None \
                    and not any(book.shares.values()):
                drawdown_halt["exit_date"] = day

        equity = book.cash + sum((book.shares[s] * bars[s].close for s in symbols), Decimal(0))
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak
        worst = max(worst, drawdown)
        invested_days += any(book.shares.values())
        equity_curve.append({"date": day, "cash": str(book.cash), "equity": str(equity),
                             "positions": dict(book.shares)})
        if config.drawdown_halt and not halted and drawdown >= config.max_drawdown_fraction:
            halted = True
            drawdown_halt = {"threshold_pct": str(config.max_drawdown_fraction * 100),
                             "trigger_date": day,
                             "trigger_drawdown_pct": _pct(drawdown),
                             "positions_at_trigger": dict(book.shares),
                             "exit_date": None if any(book.shares.values()) else day}
            targets = dict.fromkeys(symbols, Decimal(0))
            pending = set(symbols)
            decisions.append({"date": day, "symbol": None, "action": "HALT_NEW_ENTRIES",
                              "reason": "DRAWDOWN_LIMIT"})

        if index in signals_at:
            views = {s: History(rows[s], index) for s in symbols}
            weights = _checked_weights(allocator(views), symbols)
            if not halted:
                targets = weights
                pending = set(symbols)
            signals.append({"date": day,
                            "weights": {s: str(w) for s, w in weights.items()},
                            "reason": "DRAWDOWN_HALT" if halted else "SIGNAL"})

    for symbol in symbols:
        ledger = sum(fill["shares"] * (1 if fill["side"] == "BUY" else -1)
                     for fill in book.fills if fill["symbol"] == symbol)
        if ledger != book.shares[symbol]:
            raise RuntimeError(f"{symbol} position does not match the fill ledger")
    if drawdown_halt is not None:
        drawdown_halt["exit_pending"] = any(book.shares.values())
    last = {s: universe.bars(s)[end - 1].close for s in symbols}
    start_cash = config.starting_cash
    total_pnl = equity - start_cash
    return {
        "mode": "portfolio_simulation", "symbols": list(symbols),
        "first_date": universe.dates[start].isoformat(),
        "last_date": universe.dates[end - 1].isoformat(), "bars": end - start,
        "slippage_per_share": str(config.slippage_per_share),
        "starting_cash": str(start_cash), "ending_cash": str(book.cash),
        "ending_equity": str(equity), "total_pnl": str(total_pnl),
        "total_return_pct": _pct(total_pnl / start_cash),
        "max_drawdown_pct": _pct(worst),
        "open_positions": dict(book.shares), "halted": halted,
        "drawdown_halt": drawdown_halt,
        "turnover_pct": _pct(book.traded / start_cash),
        # Each asset's net cash flow plus its ending value; these add up to the
        # total return because idle cash earns nothing.
        "contribution_pct": {s: _pct((book.flow[s] + book.shares[s] * last[s]) / start_cash)
                             for s in symbols},
        "metrics": performance(start_cash, [Decimal(row["equity"]) for row in equity_curve],
                               universe.dates[start], universe.dates[end - 1],
                               book.closed_trades, invested_days),
        "signals": signals, "decisions": decisions, "fills": book.fills,
        "unfilled_orders": unfilled, "closed_trades": book.closed_trades,
        "equity_curve": equity_curve,
    }


def _equal_weights(symbols: tuple[str, ...]) -> dict[str, Decimal]:
    # Rounded down so the six weights never add up to more than 100%.
    weight = (Decimal(1) / len(symbols)).quantize(Decimal("1e-12"), rounding=ROUND_DOWN)
    return dict.fromkeys(symbols, weight)


def benchmark_equal_weight(universe: Universe, config: PortfolioConfig = PortfolioConfig(),
                           start: int = 0, end: int | None = None) -> dict:
    """B1: equal weights on every signal day, same costs and band, no halt."""
    weights = _equal_weights(universe.symbols)
    return simulate_portfolio(universe, lambda _views: weights,
                              replace(config, drawdown_halt=False), start=start, end=end)


def benchmark_spy(universe: Universe, config: PortfolioConfig = PortfolioConfig(),
                  start: int = 0, end: int | None = None) -> dict:
    """B2: 100% SPY bought at the first signal and held, same costs, no halt."""
    if "SPY" not in universe.symbols:
        raise ValueError("Benchmark B2 needs SPY in the universe")
    return simulate_portfolio(universe, lambda _views: {"SPY": Decimal(1)},
                              replace(config, drawdown_halt=False), start=start, end=end,
                              schedule=[start])

