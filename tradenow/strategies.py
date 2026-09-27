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


# The registered GLD candidates. ADR-010 fixes this list before any new
# candidate is evaluated; nothing outside it counts toward the gate.
GLD_CANDIDATES: tuple[Candidate, ...] = (sma(3, 10), sma(5, 20), sma(10, 30))


def candidate_named(name: str | None) -> Candidate | None:
    return next((item for item in GLD_CANDIDATES if item.name == name), None)
