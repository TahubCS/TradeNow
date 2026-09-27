"""Versioned, point-in-time daily features for GLD (Phase 2).

compute_features returns one row per bar. The row for day t uses only bars up
to and including t, so it is exactly what was knowable at that close. A test
checks this directly: computing on history cut off at t gives the same row as
computing on the full history. A value is None until enough history exists
(the warmup).

Changing any definition here requires a new FEATURE_VERSION, and the
feature code hash changes automatically, so every report and experiment
records which features it used.
"""

import hashlib
from bisect import insort
from collections import deque
from datetime import date
from decimal import Decimal
from pathlib import Path

from .equity_types import EquityBar, FeatureRow


FEATURE_VERSION = "gld_features_v1"
TRADING_DAYS = Decimal(252)
RETURN_WINDOWS = (1, 5, 20, 60, 126, 252)
SMA_WINDOWS = (10, 20, 50, 200)
SKIP_RECENT = 21  # momentum skips the most recent month
FEATURE_NAMES = (
    *(f"ret_{n}" for n in RETURN_WINDOWS), "mom_6_1", "mom_12_1",
    *(f"sma_{n}" for n in SMA_WINDOWS), "dist_sma_200",
    "rsi_14", "atr_14", "vol_20", "vol_60", "vol_20_median_252",
    "volume_z_20", "donchian_high_55", "donchian_low_20", "drawdown_252",
)


def feature_code_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    return numerator / denominator - 1


def _std(values: list[Decimal]) -> Decimal:
    mean = sum(values, Decimal(0)) / len(values)
    return (sum(((value - mean) ** 2 for value in values), Decimal(0))
            / (len(values) - 1)).sqrt()


class _Wilder:
    """Wilder's smoothing: a simple average to start, then (prior*(n-1) + x)/n."""

    def __init__(self, period: int):
        self.period = period
        self.seed: list[Decimal] = []
        self.value: Decimal | None = None

    def update(self, x: Decimal) -> Decimal | None:
        if self.value is None:
            self.seed.append(x)
            if len(self.seed) == self.period:
                self.value = sum(self.seed, Decimal(0)) / self.period
            return self.value
        self.value = (self.value * (self.period - 1) + x) / self.period
        return self.value


def compute_features(bars: list[EquityBar]) -> list[FeatureRow]:
    """One row per bar; row t depends only on bars[0..t]."""
    closes = [bar.close for bar in bars]
    returns: list[Decimal] = []
    gains, losses, true_range = _Wilder(14), _Wilder(14), _Wilder(14)
    vol20_history: deque[Decimal] = deque()
    vol20_sorted: list[Decimal] = []
    rows = []
    for t, bar in enumerate(bars):
        values: dict[str, Decimal | None] = dict.fromkeys(FEATURE_NAMES)
        if t:
            previous = closes[t - 1]
            returns.append(_ratio(bar.close, previous))
            change = bar.close - previous
            gain = gains.update(max(change, Decimal(0)))
            loss = losses.update(max(-change, Decimal(0)))
            if gain is not None and loss is not None:
                values["rsi_14"] = (Decimal(100) if loss == 0
                                    else 100 - 100 / (1 + gain / loss))
            span = max(bar.high, previous) - min(bar.low, previous)
            values["atr_14"] = true_range.update(span)
        for n in RETURN_WINDOWS:
            if t >= n:
                values[f"ret_{n}"] = _ratio(bar.close, closes[t - n])
        if t >= 126:
            values["mom_6_1"] = _ratio(closes[t - SKIP_RECENT], closes[t - 126])
        if t >= 252:
            values["mom_12_1"] = _ratio(closes[t - SKIP_RECENT], closes[t - 252])
        for n in SMA_WINDOWS:
            if t + 1 >= n:
                values[f"sma_{n}"] = sum(closes[t + 1 - n:t + 1], Decimal(0)) / n
        sma_200 = values["sma_200"]
        if sma_200 is not None:
            values["dist_sma_200"] = _ratio(bar.close, sma_200)
        for n in (20, 60):
            if len(returns) >= n:
                values[f"vol_{n}"] = _std(returns[-n:]) * TRADING_DAYS.sqrt()
        vol_20 = values["vol_20"]
        if vol_20 is not None:
            vol20_history.append(vol_20)
            insort(vol20_sorted, vol_20)
            if len(vol20_history) > 252:
                vol20_sorted.remove(vol20_history.popleft())
            if len(vol20_history) == 252:
                middle = len(vol20_sorted) // 2
                values["vol_20_median_252"] = (vol20_sorted[middle - 1]
                                               + vol20_sorted[middle]) / 2
        if t + 1 >= 20:
            recent = [Decimal(item.volume) for item in bars[t + 1 - 20:t + 1]]
            deviation = _std(recent)
            if deviation > 0:
                values["volume_z_20"] = ((Decimal(bar.volume)
                                          - sum(recent, Decimal(0)) / 20) / deviation)
        # Donchian channels use the previous days only, so today's bar can break them.
        if t >= 55:
            values["donchian_high_55"] = max(item.high for item in bars[t - 55:t])
        if t >= 20:
            values["donchian_low_20"] = min(item.low for item in bars[t - 20:t])
        if t + 1 >= 252:
            values["drawdown_252"] = _ratio(bar.close, max(closes[t + 1 - 252:t + 1]))
        rows.append(FeatureRow(bar.date, bar.close, values))
    return rows


def snapshot(bars: list[EquityBar], day: date) -> dict:
    """The feature row for one date, computed only from bars up to that date."""
    history = [bar for bar in bars if bar.date <= day]
    if not history or history[-1].date != day:
        raise ValueError(f"No GLD bar on {day}")
    row = compute_features(history)[-1]
    return {"feature_version": FEATURE_VERSION, "feature_code_sha256": feature_code_sha256(),
            "date": row.date.isoformat(), "close": str(row.close),
            "features": {name: None if value is None else str(value)
                         for name, value in row.values.items()}}
