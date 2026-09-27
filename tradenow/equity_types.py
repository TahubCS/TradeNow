"""Plain data types shared by the simulator, features, and strategies (no dependencies)."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class EquityBar:
    date: date
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class FeatureRow:
    """Features known at one day's close. Values are None until warmed up."""
    date: date
    close: Decimal
    values: dict[str, Decimal | None]

    def __getitem__(self, name: str) -> Decimal | None:
        return self.values[name]
