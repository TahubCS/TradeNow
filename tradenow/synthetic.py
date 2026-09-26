"""Deterministic, fictional daily bars for an offline MGC-shaped market."""

import csv
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from random import Random

from .market_data import Bar


def generate_bars(seed: int = 3, days: int = 360) -> list[Bar]:
    if days < 180:
        raise ValueError("Offline research requires at least 180 simulated bars")

    rng = Random(seed)
    current_date = date(2025, 1, 2)
    prior_close = Decimal("2000.0")
    bars: list[Bar] = []
    # Alternating trends and choppy periods exercise both signals and risk limits.
    drifts = (Decimal("0.8"), Decimal("-0.7"), Decimal("0.5"), Decimal("0"))

    while len(bars) < days:
        if current_date.weekday() < 5:
            index = len(bars)
            regime = min(3, index * 4 // days)
            opening = prior_close + Decimal(rng.randint(-5, 5)) / 10
            closing = max(Decimal("100"), opening + drifts[regime]
                          + Decimal(rng.randint(-40, 40)) / 10)
            high = max(opening, closing) + Decimal(rng.randint(1, 15)) / 10
            low = min(opening, closing) - Decimal(rng.randint(1, 15)) / 10
            bars.append(Bar(current_date, "MGC_SIM", opening, high, low, closing,
                            rng.randint(1000, 5000)))
            prior_close = closing
        current_date += timedelta(days=1)

    return bars


def bars_to_csv(bars: list[Bar]) -> str:
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("date", "contract", "open", "high", "low", "close", "volume"))
    for bar in bars:
        writer.writerow((bar.date.isoformat(), bar.contract, bar.open, bar.high,
                         bar.low, bar.close, bar.volume))
    return output.getvalue()


def validate_synthetic_bars(bars: list[Bar]) -> None:
    """Check assumptions of this weekday-only fictional feed, not real markets."""
    for previous, current in zip(bars, bars[1:]):
        expected = previous.date + timedelta(days=1)
        while expected.weekday() >= 5:
            expected += timedelta(days=1)
        if current.date != expected:
            raise ValueError(f"Missing or unexpected synthetic weekday before {current.date}")
        if not Decimal("0.90") <= current.close / previous.close <= Decimal("1.10"):
            raise ValueError(f"Synthetic price jump exceeds 10% on {current.date}")
