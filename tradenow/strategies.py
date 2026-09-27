"""The strategy interface (Phase 3): features known at a close -> target position.

A strategy sees history only through History, a read-only view that ends at
the current day, so it cannot read a future bar even by mistake. It returns a
Decision: a target between 0 (cash) and 1 (the full position the risk limits
allow), plus the evidence it used, which is logged with every signal.

The same strategy object drives the backtest (simulate_equity), the rolling
evaluation, and paper-plan, so the rule tested is the rule traded.

Sizing is decided at entry only: a fractional target sets how much to buy
when flat, and the position is not rebalanced while held. Any target of zero
closes it.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, overload

from .equity_types import FeatureRow


@dataclass(frozen=True)
class Decision:
    target: Decimal
    evidence: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.target.is_finite() or not 0 <= self.target <= 1:
            raise ValueError("Strategy target must be between 0 and 1")


class History(Sequence[FeatureRow]):
    """Rows up to and including one day. Negative indexes count back from that day."""

    def __init__(self, rows: Sequence[FeatureRow], end: int):
        if not 0 <= end < len(rows):
            raise IndexError("History end is outside the rows")
        self._rows, self._end = rows, end

    def __len__(self) -> int:
        return self._end + 1

    @overload
    def __getitem__(self, index: int) -> FeatureRow: ...

    @overload
    def __getitem__(self, index: slice) -> list[FeatureRow]: ...

    def __getitem__(self, index: int | slice) -> FeatureRow | list[FeatureRow]:
        if isinstance(index, slice):
            return [self._rows[i] for i in range(*index.indices(len(self)))]
        position = index + len(self) if index < 0 else index
        if not 0 <= position < len(self):
            raise IndexError("History index is outside the known days")
        return self._rows[position]

    def __iter__(self) -> Iterator[FeatureRow]:
        return (self._rows[i] for i in range(len(self)))

    @property
    def today(self) -> FeatureRow:
        return self._rows[self._end]


class Strategy(Protocol):
    @property
    def name(self) -> str: ...

    # False lets the simulator skip computing the full feature set.
    @property
    def needs_features(self) -> bool: ...

    def decide(self, history: History, holding: bool) -> Decision | None:
        """Target after today's close, or None while still warming up."""
        ...


def _mean_close(history: History, window: int) -> Decimal:
    return sum((row.close for row in history[-window:]), Decimal(0)) / window


@dataclass(frozen=True)
class SmaCross:
    """Long when the fast simple moving average of closes is above the slow one."""
    fast: int
    slow: int
    needs_features: bool = False

    def __post_init__(self) -> None:
        if not 0 < self.fast < self.slow:
            raise ValueError("Expected 0 < fast < slow")

    @property
    def name(self) -> str:
        return f"sma_{self.fast}_{self.slow}"

    def decide(self, history: History, holding: bool) -> Decision | None:
        if len(history) < self.slow:
            return None
        fast, slow = _mean_close(history, self.fast), _mean_close(history, self.slow)
        return Decision(Decimal(int(fast > slow)),
                        {"fast_sma": str(fast), "slow_sma": str(slow)})


def _values(history: History, *names: str) -> list[Decimal] | None:
    """Today's feature values, or None while any is still warming up."""
    values = [history.today[name] for name in names]
    return None if any(value is None for value in values) else values  # type: ignore[return-value]


LONG, FLAT = Decimal(1), Decimal(0)


@dataclass(frozen=True)
class TimeSeriesMomentum:
    """Long when the return over the past 6 or 12 months, skipping the most
    recent month, is positive."""
    months: int
    needs_features: bool = True

    def __post_init__(self) -> None:
        if self.months not in (6, 12):
            raise ValueError("Momentum lookback must be 6 or 12 months")

    @property
    def name(self) -> str:
        return f"tsmom_{self.months}_1"

    def decide(self, history: History, holding: bool) -> Decision | None:
        values = _values(history, f"mom_{self.months}_1")
        if values is None:
            return None
        return Decision(LONG if values[0] > 0 else FLAT, {"momentum": str(values[0])})


@dataclass(frozen=True)
class Trend200:
    """Long when the close is above its 200-day average; with the filter, only
    while 20-day volatility is below its median of the past year."""
    volatility_filter: bool
    needs_features: bool = True

    @property
    def name(self) -> str:
        return "trend_200_volfilter" if self.volatility_filter else "trend_200"

    def decide(self, history: History, holding: bool) -> Decision | None:
        names = ["dist_sma_200"] + (["vol_20", "vol_20_median_252"]
                                    if self.volatility_filter else [])
        values = _values(history, *names)
        if values is None:
            return None
        long = values[0] > 0 and (not self.volatility_filter or values[1] < values[2])
        return Decision(LONG if long else FLAT,
                        {name: str(value) for name, value in zip(names, values, strict=True)})


@dataclass(frozen=True)
class Donchian:
    """Enter when the close breaks the prior N-day high; exit when it breaks
    the prior M-day low."""
    entry: int
    exit: int
    needs_features: bool = True

    def __post_init__(self) -> None:
        if (self.entry, self.exit) not in ((55, 20), (20, 10)):
            raise ValueError("Registered Donchian channels are 55/20 and 20/10")

    @property
    def name(self) -> str:
        return f"donchian_{self.entry}_{self.exit}"

    def decide(self, history: History, holding: bool) -> Decision | None:
        values = _values(history, f"donchian_high_{self.entry}", f"donchian_low_{self.exit}")
        if values is None:
            return None
        high, low = values
        close = history.today.close
        long = close >= low if holding else close > high
        return Decision(LONG if long else FLAT,
                        {"channel_high": str(high), "channel_low": str(low)})


@dataclass(frozen=True)
class VolatilityTarget:
    """Size another strategy's entries to a target annual volatility, never
    above the full allowed position: size = min(1, target / 20-day volatility)."""
    inner: Strategy
    target_volatility: Decimal = Decimal("0.15")
    needs_features: bool = True

    @property
    def name(self) -> str:
        return f"{self.inner.name}_vt{int(self.target_volatility * 100)}"

    def decide(self, history: History, holding: bool) -> Decision | None:
        decision = self.inner.decide(history, holding)
        volatility = history.today["vol_20"]
        if decision is None or volatility is None:
            return None
        size = (Decimal(1) if volatility <= 0
                else min(Decimal(1), self.target_volatility / volatility))
        return Decision((decision.target * size).quantize(Decimal("0.0001")),
                        {**decision.evidence, "vol_20": str(volatility),
                         "size": str(size.quantize(Decimal("0.0001")))})


@dataclass(frozen=True)
class Candidate:
    """A registered strategy with the parameters recorded in reports."""
    strategy: Strategy
    parameters: dict[str, int | str]

    @property
    def name(self) -> str:
        return self.strategy.name


def sma(fast: int, slow: int) -> Candidate:
    return Candidate(SmaCross(fast, slow), {"fast_window": fast, "slow_window": slow})


def registered(strategy: Strategy, **parameters: int | str) -> Candidate:
    return Candidate(strategy, parameters)


# The registered GLD candidates (ADR-010). This list was fixed before any of
# the new candidates was evaluated on real data; nothing outside it counts
# toward the live-trading gate, and changing it needs a new ADR.
GLD_CANDIDATES: tuple[Candidate, ...] = (
    sma(3, 10), sma(5, 20), sma(10, 30),
    registered(TimeSeriesMomentum(6), lookback_months=6),
    registered(TimeSeriesMomentum(12), lookback_months=12),
    registered(Trend200(False), average_days=200),
    registered(Trend200(True), average_days=200, volatility_filter="20d below 1y median"),
    registered(Donchian(55, 20), entry_days=55, exit_days=20),
    registered(Donchian(20, 10), entry_days=20, exit_days=10),
    registered(VolatilityTarget(TimeSeriesMomentum(12)), target_volatility="0.15"),
    registered(VolatilityTarget(Trend200(False)), target_volatility="0.15"),
    registered(VolatilityTarget(Donchian(55, 20)), target_volatility="0.15"),
)


def candidate_named(name: str | None) -> Candidate | None:
    return next((item for item in GLD_CANDIDATES if item.name == name), None)
