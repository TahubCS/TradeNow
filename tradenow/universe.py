"""The registered multi-asset universe (ADR-011) and its aligned price history.

Research uses Tiingo's dividend- and split-adjusted prices, so holding an ETF
earns its total return. Raw closes are kept beside them for execution checks.
All symbols must share one trading calendar: the history is trimmed to the
range every symbol covers, and a day missing for any symbol inside that range
is an error, never filled in.
"""

import csv
import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path

from .equity_types import EquityBar
from .tiingo import PRIVATE_DIR, latest_import


UNIVERSE = ("GLD", "SLV", "SPY", "EFA", "IEF", "DBC")
MAX_CSV_BYTES = 3_000_000
# Enough for one 504/126/126-bar rolling window (ADR-011).
MIN_COMMON_BARS = 756
REQUIRED = ("date", "symbol", "open", "high", "low", "close", "volume",
            "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume",
            "div_cash", "split_factor")


@dataclass(frozen=True)
class AssetHistory:
    symbol: str
    bars: list[EquityBar]  # adjusted OHLC, raw volume
    raw_close: dict[date, Decimal]
    dividends: int
    splits: int
    sha256: str
    source_name: str


@dataclass(frozen=True)
class Universe:
    symbols: tuple[str, ...]
    dates: list[date]
    assets: dict[str, AssetHistory]  # every asset's bars share exactly `dates`
    sha256: str  # identifies the combined inputs

    def bars(self, symbol: str) -> list[EquityBar]:
        return self.assets[symbol].bars


def parse_adjusted_csv(source_bytes: bytes, symbol: str,
                       source_name: str = "") -> AssetHistory:
    """One symbol's Tiingo CSV as adjusted daily bars."""
    if len(source_bytes) > MAX_CSV_BYTES:
        raise ValueError(f"{symbol} CSV exceeds {MAX_CSV_BYTES} bytes")
    try:
        reader = csv.DictReader(StringIO(source_bytes.decode("utf-8-sig")))
    except UnicodeDecodeError:
        raise ValueError(f"{symbol} CSV must be UTF-8 encoded") from None
    if not reader.fieldnames or not set(REQUIRED).issubset(reader.fieldnames):
        raise ValueError(f"{symbol} CSV must contain Tiingo raw and adjusted daily columns")
    bars: list[EquityBar] = []
    raw_close: dict[date, Decimal] = {}
    dividends = splits = 0
    for number, row in enumerate(reader, start=2):
        try:
            day = date.fromisoformat(row["date"])
            raw = [Decimal(row[name]) for name in ("open", "high", "low", "close")]
            adjusted = [Decimal(row[name]) for name in
                        ("adj_open", "adj_high", "adj_low", "adj_close")]
            volume = int(row["volume"])
            dividend, split = Decimal(row["div_cash"]), Decimal(row["split_factor"])
        except (TypeError, ValueError, InvalidOperation):
            raise ValueError(f"Invalid {symbol} CSV row {number}") from None
        if row["symbol"] != symbol:
            raise ValueError(f"{symbol} CSV row {number} is for {row['symbol']}")
        for prices in (raw, adjusted):
            if (any(not price.is_finite() or price <= 0 for price in prices)
                    or prices[2] > min(prices[0], prices[3])
                    or prices[1] < max(prices[0], prices[3])):
                raise ValueError(f"Invalid {symbol} prices on row {number}")
        if volume < 0 or not dividend.is_finite() or dividend < 0 or not split > 0:
            raise ValueError(f"Invalid {symbol} volume or corporate action on row {number}")
        if day.weekday() >= 5 or (bars and day <= bars[-1].date):
            raise ValueError(f"{symbol} dates must be increasing weekdays (row {number})")
        dividends += dividend > 0
        splits += split != 1
        bars.append(EquityBar(day, symbol, adjusted[0], adjusted[1], adjusted[2],
                              adjusted[3], volume))
        raw_close[day] = raw[3]
    if not bars:
        raise ValueError(f"{symbol} CSV has no bars")
    return AssetHistory(symbol, bars, raw_close, dividends, splits,
                        hashlib.sha256(source_bytes).hexdigest(), source_name)


def align(histories: list[AssetHistory]) -> Universe:
    """Trim every history to the common range and require identical trading days."""
    if not histories:
        raise ValueError("No symbols to align")
    start = max(history.bars[0].date for history in histories)
    end = min(history.bars[-1].date for history in histories)
    if start > end:
        raise ValueError("The symbols have no dates in common")
    trimmed = {history.symbol: [bar for bar in history.bars if start <= bar.date <= end]
               for history in histories}
    calendar = sorted({bar.date for bars in trimmed.values() for bar in bars})
    problems = []
    for symbol, bars in trimmed.items():
        missing = sorted(set(calendar) - {bar.date for bar in bars})
        if missing:
            problems.append(f"{symbol} is missing {len(missing)} day(s), first "
                            + ", ".join(day.isoformat() for day in missing[:3]))
    if problems:
        raise ValueError("Symbols do not share one trading calendar: " + "; ".join(problems))
    if len(calendar) < MIN_COMMON_BARS:
        raise ValueError(f"Only {len(calendar)} common days; at least {MIN_COMMON_BARS} "
                         "are needed for one rolling window")
    assets = {history.symbol: AssetHistory(
        history.symbol, trimmed[history.symbol],
        {day: close for day, close in history.raw_close.items() if start <= day <= end},
        history.dividends, history.splits, history.sha256, history.source_name)
        for history in histories}
    combined = hashlib.sha256("".join(f"{history.symbol}:{history.sha256}\n"
                                      for history in histories).encode()).hexdigest()
    return Universe(tuple(history.symbol for history in histories), calendar, assets,
                    combined)


def load_universe(directory: Path = PRIVATE_DIR,
                  symbols: tuple[str, ...] = UNIVERSE) -> Universe:
    """The newest verified import of every symbol, aligned."""
    histories = []
    for symbol in symbols:
        source, name = latest_import(symbol, directory, MAX_CSV_BYTES)
        histories.append(parse_adjusted_csv(source, symbol, name))
    return align(histories)


def describe(universe: Universe) -> dict:
    """A short summary for the command line."""
    return {"symbols": list(universe.symbols), "first_date": universe.dates[0],
            "last_date": universe.dates[-1], "common_days": len(universe.dates),
            "sha256": universe.sha256,
            "assets": {symbol: {"source": asset.source_name, "sha256": asset.sha256,
                                "dividends": asset.dividends, "splits": asset.splits}
                       for symbol, asset in universe.assets.items()}}
