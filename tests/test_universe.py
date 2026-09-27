import hashlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tradenow.tiingo import HEADER, import_symbol, latest_import, prune_imports
from tradenow.universe import (
    MIN_COMMON_BARS,
    UNIVERSE,
    align,
    load_universe,
    parse_adjusted_csv,
)


def weekdays(start: date, count: int, skip: set[date] = frozenset()) -> list[date]:
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5 and day not in skip:
            days.append(day)
        day += timedelta(days=1)
    return days


def tiingo_csv(symbol: str, days: list[date], dividend_on: date | None = None) -> bytes:
    """Raw prices with a dividend: adjusted prices before it are scaled down 2%."""
    rows = [",".join(HEADER)]
    for index, day in enumerate(days):
        close = Decimal(100 + index % 17)
        factor = Decimal("0.98") if dividend_on and day < dividend_on else Decimal(1)
        raw = (close, close + 1, close - 1, close)
        adjusted = tuple(price * factor for price in raw)
        dividend = "1.5" if day == dividend_on else "0.0"
        rows.append(",".join([day.isoformat(), symbol, *map(str, raw), "5000",
                              *map(str, adjusted), "5000", dividend, "1.0"]))
    return ("\n".join(rows) + "\n").encode()


def save_import(directory: Path, symbol: str, data: bytes, start: date, end: date) -> None:
    stem = f"{symbol}-{start:%Y%m%d}-{end:%Y%m%d}"
    (directory / f"{stem}.csv").write_bytes(data)
    (directory / f"{stem}.raw.json").write_text("[]", encoding="utf-8")
    (directory / f"{stem}.manifest.json").write_text(json.dumps({
        "provider": "Tiingo", "symbol": symbol, "last_bar": end.isoformat(),
        "bars_sha256": hashlib.sha256(data).hexdigest()}), encoding="utf-8")


class ImportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def test_any_registered_symbol_imports_with_its_own_files(self):
        rows = [{"date": "2026-09-24T00:00:00.000Z", "open": 30.0, "high": 31.0, "low": 29.0,
                 "close": 30.5, "volume": 900, "adjOpen": 29.0, "adjHigh": 30.0,
                 "adjLow": 28.0, "adjClose": 29.5, "adjVolume": 900, "divCash": 0.2,
                 "splitFactor": 1.0}]
        meta = {"ticker": "slv", "startDate": "2006-04-28", "endDate": "2026-09-24"}
        with patch("tradenow.tiingo._get_json",
                   side_effect=[(meta, b"{}"), (rows, json.dumps(rows).encode())]) as get:
            result = import_symbol("SLV", date(2026, 9, 24), date(2026, 9, 24), "k",
                                   self.directory)
        self.assertEqual(get.call_args_list[0].args[0], "https://api.tiingo.com/tiingo/daily/SLV")
        self.assertEqual(result["symbol"], "SLV")
        self.assertTrue(Path(result["bars_file"]).name.startswith("SLV-20260924-20260924"))
        source, name = latest_import("SLV", self.directory)
        self.assertIn(b",SLV,", source)
        with self.assertRaises(ValueError):
            latest_import("GLD", self.directory)

    def test_symbols_must_be_plain_tickers(self):
        for bad in ("gld", "GLD;rm", "../GLD", "TOOLONG"):
            with self.subTest(symbol=bad), self.assertRaisesRegex(ValueError, "ticker"):
                import_symbol(bad, date(2026, 1, 2), date(2026, 1, 2), "k", self.directory)

    def test_pruning_keeps_the_newest_imports_of_that_symbol_only(self):
        start = date(2004, 11, 18)
        ends = [date(2026, 9, day) for day in (21, 22, 23, 24, 25)]
        for end in ends:
            save_import(self.directory, "GLD", b"x", start, end)
        save_import(self.directory, "SLV", b"y", start, ends[0])
        archive = self.directory / "superseded"
        archive.mkdir()
        for stamp in ("20260921", "20260922", "20260923", "20260924"):
            (archive / f"{stamp}-GLD-20041118-20260925.csv").write_bytes(b"old")
        (archive / "20260920-SLV-20041118-20260925.csv").write_bytes(b"old")
        deleted = prune_imports("GLD", self.directory, keep=3)
        remaining = sorted(path.name for path in self.directory.glob("GLD-*.manifest.json"))
        self.assertEqual(remaining, [f"GLD-20041118-2026092{day}.manifest.json"
                                     for day in (3, 4, 5)])
        self.assertEqual(len(deleted), 2 * 3 + 1)
        self.assertTrue((self.directory / "SLV-20041118-20260921.csv").exists())
        self.assertTrue((archive / "20260920-SLV-20041118-20260925.csv").exists())
        self.assertFalse((archive / "20260921-GLD-20041118-20260925.csv").exists())
        with self.assertRaises(ValueError):
            prune_imports("GLD", self.directory, keep=0)


class UniverseTests(unittest.TestCase):
    def test_adjusted_prices_include_dividends(self):
        days = weekdays(date(2020, 1, 6), 10)
        history = parse_adjusted_csv(tiingo_csv("SPY", days, dividend_on=days[5]), "SPY")
        self.assertEqual(history.dividends, 1)
        self.assertEqual(history.bars[0].close, Decimal(100) * Decimal("0.98"))
        self.assertEqual(history.raw_close[days[0]], Decimal(100))
        self.assertEqual(history.bars[5].close, Decimal(105))
        with self.assertRaisesRegex(ValueError, "is for SPY"):
            parse_adjusted_csv(tiingo_csv("SPY", days), "IEF")

    def test_alignment_trims_edges_and_refuses_gaps(self):
        days = weekdays(date(2010, 1, 4), MIN_COMMON_BARS + 40)
        early = parse_adjusted_csv(tiingo_csv("GLD", days), "GLD")
        late = parse_adjusted_csv(tiingo_csv("SLV", days[20:]), "SLV")
        short = parse_adjusted_csv(tiingo_csv("DBC", days[:-5]), "DBC")
        universe = align([early, late, short])
        self.assertEqual(universe.dates, days[20:-5])
        self.assertEqual({len(universe.bars(s)) for s in universe.symbols}, {len(days) - 25})
        gap_days = [day for day in days if day != days[100]]
        gappy = parse_adjusted_csv(tiingo_csv("IEF", gap_days), "IEF")
        with self.assertRaisesRegex(ValueError, f"IEF is missing 1 day.*{days[100]}"):
            align([early, gappy])
        tiny = parse_adjusted_csv(tiingo_csv("SPY", days[:100]), "SPY")
        with self.assertRaisesRegex(ValueError, "common days"):
            align([early, tiny])

    def test_loads_every_registered_symbol_and_hashes_the_inputs(self):
        days = weekdays(date(2010, 1, 4), MIN_COMMON_BARS + 10)
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            for symbol in UNIVERSE:
                save_import(directory, symbol, tiingo_csv(symbol, days), days[0], days[-1])
            universe = load_universe(directory)
            self.assertEqual(universe.symbols, UNIVERSE)
            self.assertEqual(load_universe(directory).sha256, universe.sha256)
            save_import(directory, "SPY", tiingo_csv("SPY", days, days[3]), days[0],
                        days[-1] + timedelta(days=7))
            self.assertNotEqual(load_universe(directory).sha256, universe.sha256)


if __name__ == "__main__":
    unittest.main()
