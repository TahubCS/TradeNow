"""Load and validate a single contract's daily bars from a local CSV."""

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path


@dataclass(frozen=True)
class Bar:
    date: date
    contract: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def load_bars(path: Path) -> list[Bar]:
    required = {"date", "contract", "open", "high", "low", "close", "volume"}
    bars: list[Bar] = []

    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"CSV must contain: {', '.join(sorted(required))}")

        for row_number, row in enumerate(reader, start=2):
            try:
                bar = Bar(
                    date=date.fromisoformat(row["date"]),
                    contract=(row["contract"] or "").strip(),
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=int(row["volume"]),
                )
            except (TypeError, ValueError, InvalidOperation) as error:
                raise ValueError(f"Invalid value on CSV row {row_number}: {error}") from error

            if not bar.contract:
                raise ValueError(f"Missing contract on CSV row {row_number}")
            if any(not price.is_finite() or price <= 0 for price in
                   (bar.open, bar.high, bar.low, bar.close)):
                raise ValueError(f"Prices must be finite and positive on CSV row {row_number}")
            if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
                raise ValueError(f"Invalid OHLC range on CSV row {row_number}")
            if bar.volume < 0:
                raise ValueError(f"Negative volume on CSV row {row_number}")
            if bars and bar.date <= bars[-1].date:
                raise ValueError(f"Dates must be unique and increasing on CSV row {row_number}")
            if bars and bar.contract != bars[0].contract:
                raise ValueError("This first slice supports one contract per CSV")
            bars.append(bar)

    if not bars:
        raise ValueError("CSV contains no bars")
    return bars
