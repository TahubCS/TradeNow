"""Load and validate daily MGC bars from a local CSV."""

import csv
import datetime as dt
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TextIO


@dataclass(frozen=True)
class Bar:
    date: date
    contract: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    last_trade_date: dt.date | None = None
    open_time_ct: datetime | None = None


def load_bars(path: Path) -> list[Bar]:
    with path.open(newline="", encoding="utf-8") as source:
        return parse_bars(source)


def parse_bars(source: TextIO, allow_rolls: bool = False) -> list[Bar]:
    required = {"date", "contract", "open", "high", "low", "close", "volume"}
    bars: list[Bar] = []

    reader = csv.DictReader(source)
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError(f"CSV must contain: {', '.join(sorted(required))}")

    seen_contracts: set[str] = set()
    expiry_by_contract: dict[str, date | None] = {}

    for row_number, row in enumerate(reader, start=2):
        try:
            last_trade_date = (date.fromisoformat(row["last_trade_date"])
                               if row.get("last_trade_date") else None)
            open_time_ct = (datetime.fromisoformat(row["open_time_ct"])
                            if row.get("open_time_ct") else None)
            bar = Bar(
                date=date.fromisoformat(row["date"]),
                contract=(row["contract"] or "").strip(),
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                volume=int(row["volume"]),
                last_trade_date=last_trade_date,
                open_time_ct=open_time_ct,
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
        if bar.last_trade_date and bar.date >= bar.last_trade_date:
            raise ValueError(f"Bar reaches contract last trade date on CSV row {row_number}")
        if bar.open_time_ct:
            offset = bar.open_time_ct.utcoffset()
            if offset not in (timedelta(hours=-5), timedelta(hours=-6)):
                raise ValueError(f"open_time_ct needs a Chicago UTC offset on CSV row {row_number}")
            trading_date = (bar.open_time_ct.date() + timedelta(days=1)
                            if bar.open_time_ct.hour >= 17 else bar.open_time_ct.date())
            if trading_date != bar.date:
                raise ValueError(f"open_time_ct does not match date on CSV row {row_number}")
        if bars and bar.date <= bars[-1].date:
            raise ValueError(f"Dates must be unique and increasing on CSV row {row_number}")
        if bars and bar.contract != bars[-1].contract:
            if not allow_rolls:
                raise ValueError("This first slice supports one contract per CSV")
            if bar.contract in seen_contracts:
                raise ValueError(f"Contract returns after a roll on CSV row {row_number}")
            seen_contracts.add(bars[-1].contract)
        if bar.contract in expiry_by_contract and expiry_by_contract[bar.contract] != bar.last_trade_date:
            raise ValueError(f"Inconsistent last_trade_date on CSV row {row_number}")
        expiry_by_contract[bar.contract] = bar.last_trade_date
        bars.append(bar)

    if not bars:
        raise ValueError("CSV contains no bars")
    return bars
