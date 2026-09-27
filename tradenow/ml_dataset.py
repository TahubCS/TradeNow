"""Training samples for the Phase 7 models (ADR-012), built from History views.

One sample per asset per month-end: the 13 registered features at that close,
and a label, the return to the next month-end close divided by vol_60 at the
start. Everything is read through History views that end at the signal day,
so a month-end is recognized only once the next trading day is visible, and a
label is used only if it ended before the signal day. No future bar can leak
in, whatever history the rows were computed from.

Nothing here performs I/O or imports the model libraries; values are Decimal.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .equity_types import FeatureRow
from .strategies import History


# The registered features (ADR-012): scale-free, so assets can be pooled.
FEATURES = ("ret_1", "ret_5", "ret_20", "ret_60", "ret_126", "ret_252", "mom_6_1",
            "mom_12_1", "dist_sma_200", "rsi_14", "vol_20", "vol_60", "drawdown_252")
MIN_LABEL_MONTHS = 24


@dataclass(frozen=True)
class Sample:
    symbol: str
    start: date  # the month-end whose features the sample holds
    end: date  # the next month-end, where the label ends
    features: tuple[Decimal, ...]
    label: Decimal  # next-month return divided by vol_60 at the start


def feature_vector(row: FeatureRow) -> tuple[Decimal, ...] | None:
    """The registered features, or None while any is still warming up."""
    values = tuple(row[name] for name in FEATURES)
    if any(value is None for value in values):
        return None
    return values  # type: ignore[return-value]


def month_end_positions(rows: list[FeatureRow]) -> list[int]:
    """Positions whose next row is in a new calendar month; the last row never
    qualifies, because its next day is not known yet."""
    return [index for index in range(len(rows) - 1)
            if (rows[index].date.year, rows[index].date.month)
            != (rows[index + 1].date.year, rows[index + 1].date.month)]


def training_samples(views: Mapping[str, History]) -> list[Sample]:
    """Every complete sample whose label ended before the views' last day."""
    samples = []
    for symbol, view in views.items():
        rows = view[:]
        ends = month_end_positions(rows)
        for start, end in zip(ends, ends[1:], strict=False):
            row = rows[start]
            vector = feature_vector(row)
            volatility = row["vol_60"]
            if vector is None or volatility is None or volatility <= 0:
                continue
            label = (rows[end].close / row.close - 1) / volatility
            samples.append(Sample(symbol, row.date, rows[end].date, vector, label))
    return samples


def label_months(samples: list[Sample]) -> int:
    return len({sample.end for sample in samples})
