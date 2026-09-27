import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_multi import synthetic_universe

from tradenow.__main__ import main
from tradenow.alpaca_paper import (
    PAPER_ENDPOINT,
    AlpacaError,
    DailyBar,
    PaperClient,
    PaperCredentials,
)
from tradenow.broad_research import BROAD_CANDIDATES, BROAD_STUDY
from tradenow.data_check import (
    check_symbol,
    require_passing_check,
    save_check,
    summarize,
)
from tradenow.equity_types import FeatureRow
from tradenow.experiments import read_experiments
from tradenow.features import FEATURE_NAMES
from tradenow.logs import close_logging
from tradenow.multi_strategies import RelativeStrength, sized_weights
from tradenow.strategies import History
from tradenow.universe import BROAD_UNIVERSE


D = Decimal


def row(**values: str | None) -> FeatureRow:
    features: dict[str, Decimal | None] = dict.fromkeys(FEATURE_NAMES)
    features.update({name: None if value is None else D(value)
                     for name, value in values.items()})
    return FeatureRow(date(2026, 1, 30), D(100), features)


def views(**rows: FeatureRow) -> dict[str, History]:
    return {symbol: History([item], 0) for symbol, item in rows.items()}


def weekdays(start: date, count: int) -> list[date]:
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


class RegistrationTests(unittest.TestCase):
    def test_universe_and_candidates_are_pinned(self):
        self.assertEqual(len(BROAD_UNIVERSE), 35)
        self.assertEqual(len(set(BROAD_UNIVERSE)), 35)
        self.assertEqual(BROAD_UNIVERSE[:4], ("SPY", "QQQ", "IWM", "MDY"))
        self.assertEqual(BROAD_UNIVERSE[-4:], ("GLD", "SLV", "DBC", "USO"))
        self.assertEqual([item.name for item in BROAD_CANDIDATES],
                         ["mom_eq", "trend_eq", "both_eq", "mom_iv10", "trend_iv10",
                          "both_iv10", "xsmom_top25", "xsmom_top25_abs", "ridge_eq", "knn_eq"])
        self.assertEqual((BROAD_STUDY.symbols, BROAD_STUDY.registered_trials_total),
                         (BROAD_UNIVERSE, 32))


class SizingTests(unittest.TestCase):
    def test_iv10_caps_at_ten_percent(self):
        # Inverse 100 and 19 x 5: A holds 0.513 before the cap; 0.9 is shared by 19.
        rows = {"A": row(vol_60="0.01"), **{f"S{i}": row(vol_60="0.2") for i in range(19)}}
        weights = sized_weights("iv10", rows, set(rows))
        self.assertEqual(weights["A"], D("0.1"))
        self.assertEqual(weights["S0"], D("0.0473684210"))
        self.assertLessEqual(sum(weights.values()), 1)

    def test_eq_divides_by_the_whole_universe(self):
        rows = {f"S{i}": row() for i in range(35)}
        self.assertEqual(sized_weights("eq", rows, {"S0"}), {"S0": D("0.0285714285")})


class RelativeStrengthTests(unittest.TestCase):
    def test_holds_the_top_quarter_with_ties_to_the_earlier_asset(self):
        rows = views(A=row(mom_12_1="0.1"), B=row(mom_12_1="0.3"), C=row(mom_12_1=None),
                     D=row(mom_12_1="0.3"), E=row(mom_12_1="-0.1"), F=row(mom_12_1="0.05"),
                     G=row(mom_12_1="0.2"), H=row(mom_12_1="-0.2"))
        self.assertEqual(RelativeStrength(False)(rows), {"B": D("0.5"), "D": D("0.5")})

    def test_absolute_filter_leaves_negative_picks_in_cash(self):
        # Five assets hold two; the second pick has negative momentum.
        rows = views(A=row(mom_12_1="-0.1"), B=row(mom_12_1="-0.3"), C=row(mom_12_1="0.05"),
                     D=row(mom_12_1=None), E=row(mom_12_1="-0.2"))
        self.assertEqual(RelativeStrength(False)(rows), {"C": D("0.5"), "A": D("0.5")})
        self.assertEqual(RelativeStrength(True)(rows), {"C": D("0.5")})


def bars(days: list[date], closes: dict[date, str] | None = None) -> list[DailyBar]:
    closes = closes or {}
    return [DailyBar(day, D(100), D(closes.get(day, "100")), 1000) for day in days]


class DataCheckTests(unittest.TestCase):
    DAYS = weekdays(date(2016, 1, 4), 200)
    TIINGO = {day: D(100) for day in DAYS}

    def check(self, alpaca: list[DailyBar]) -> dict:
        return check_symbol(self.TIINGO, alpaca, self.DAYS[0], self.DAYS[-1])

    def test_matching_closes_pass(self):
        self.assertEqual(self.check(bars(self.DAYS)),
                         {"shared_days": 200, "mismatches": 0, "passed": True, "examples": []})

    def test_mismatches_up_to_one_percent_of_shared_days(self):
        two = {self.DAYS[5]: "100.6", self.DAYS[9]: "99.4"}  # beyond 0.5%
        result = self.check(bars(self.DAYS, two))
        self.assertEqual((result["mismatches"], result["passed"]), (2, True))  # 2 <= 1% of 200
        three = {**two, self.DAYS[11]: "100.51"}
        self.assertFalse(self.check(bars(self.DAYS, three))["passed"])
        within = {self.DAYS[5]: "100.5"}
        self.assertEqual(self.check(bars(self.DAYS, within))["mismatches"], 0)

    def test_days_in_only_one_source_are_mismatches(self):
        result = self.check(bars(self.DAYS[1:] + [date(2016, 12, 31)]))
        self.assertEqual(result["mismatches"], 1)  # the extra day is outside the range
        self.assertIn(f"{self.DAYS[0]} only in Tiingo", result["examples"])

    def test_run_requires_a_passing_check_of_the_same_data(self):
        universe = synthetic_universe(800, symbols=("SPY", "GLD"))
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with self.assertRaisesRegex(ValueError, "data-check"):
                require_passing_check(universe, folder)
            passed = {"passed": True, "shared_days": 10, "mismatches": 0, "examples": []}
            failed = {**passed, "passed": False}
            save_check(summarize(universe, {"SPY": passed, "GLD": failed}), folder)
            with self.assertRaisesRegex(ValueError, "GLD"):
                require_passing_check(universe, folder)
            save_check(summarize(universe, {"SPY": passed, "GLD": passed}), folder)
            self.assertTrue(require_passing_check(universe, folder)["passed"])
            other = synthetic_universe(800, seed=2, symbols=("SPY", "GLD"))
            with self.assertRaises(ValueError):
                require_passing_check(other, folder)
        with self.assertRaises(ValueError):
            summarize(universe, {"SPY": passed})


class AlpacaHistoryTests(unittest.TestCase):
    @staticmethod
    def page(days: list[date], token: str | None) -> dict:
        return {"bars": [{"t": f"{day}T04:00:00Z", "o": 100, "c": 101, "v": 5}
                         for day in days], "next_page_token": token}

    def test_pages_are_joined_and_the_end_day_is_included(self):
        client = PaperClient(PaperCredentials(PAPER_ENDPOINT, "key-id", "secret-key"))
        days = weekdays(date(2016, 1, 4), 5)
        with patch.object(PaperClient, "_request",
                          side_effect=[self.page(days[:3], "next"), self.page(days[3:], None)]
                          ) as request:
            result = client.daily_history("SPY", days[0], days[-1])
        self.assertEqual([bar.date for bar in result], days)
        first, second = (parse_qs(urlsplit(call.args[2]).query) for call in request.call_args_list)
        self.assertEqual(first["end"], [(days[-1] + timedelta(days=1)).isoformat()])
        self.assertNotIn("page_token", first)
        self.assertEqual(second["page_token"], ["next"])

    def test_pages_must_continue_in_order(self):
        client = PaperClient(PaperCredentials(PAPER_ENDPOINT, "key-id", "secret-key"))
        days = weekdays(date(2016, 1, 4), 4)
        with patch.object(PaperClient, "_request",
                          side_effect=[self.page(days[2:], "next"), self.page(days[:2], None)]):
            with self.assertRaises(AlpacaError):
                client.daily_history("SPY", days[0], days[-1])
        with self.assertRaises(ValueError):
            client.daily_history("SPY/../v2", days[0], days[-1])


class CommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(close_logging)
        self.root = Path(directory.name)
        self.universe = synthetic_universe(950, symbols=BROAD_UNIVERSE)

    def run_main(self, *argv: str, **patches: object) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {"TRADENOW_DATA_DIR": str(self.root)}), \
                patch("tradenow.__main__.load_universe", return_value=self.universe), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            if patches:
                with patch.multiple("tradenow.__main__", **patches):
                    code = main(list(argv))
            else:
                code = main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_broad_refuses_without_a_passing_data_check(self):
        code, _, stderr = self.run_main("broad", "--output", str(self.root / "broad"))
        self.assertEqual(code, 1)
        self.assertIn("data-check", stderr)
        self.assertFalse((self.root / "experiments.jsonl").exists())

    def test_data_check_then_broad_run(self):
        class FakeClient:
            def __init__(self, credentials):
                pass

            def daily_history(client, symbol, start, end):
                return [DailyBar(day, D(1), close, 1) for day, close in
                        self.universe.assets[symbol].raw_close.items() if start <= day <= end]
        # Synthetic dates end in 2013, before the check window: a check with no
        # shared days fails, which the command reports.
        code, stdout, _ = self.run_main("data-check", PaperClient=FakeClient,
                                        load_paper_credentials=lambda: None)
        self.assertEqual((code, json.loads(stdout)["passed"]), (1, False))
        with patch("tradenow.__main__.CHECK_START", self.universe.dates[0]):
            code, stdout, _ = self.run_main("data-check", PaperClient=FakeClient,
                                            load_paper_credentials=lambda: None)
        self.assertEqual((code, json.loads(stdout)["passed"]), (0, True))
        code, stdout, _ = self.run_main("broad", "--output", str(self.root / "broad"))
        self.assertEqual(code, 0)
        printed = json.loads(stdout)
        self.assertEqual((printed["adr"], printed["candidates_evaluated"],
                          printed["registered_trials_total"]), ("ADR-013", 10, 32))
        self.assertTrue(printed["data_check"]["passed"])
        record = read_experiments(self.root / "experiments.jsonl")[0]
        self.assertEqual(record["strategy_version"], "broad_adr013_candidates_v1")

    def test_tiingo_import_broad_resumes_past_complete_symbols(self):
        imported = []

        def fake_import(symbol, start, end, key):
            imported.append(symbol)
            return {"result": "IMPORTED", "bars": 1}
        code, stdout, _ = self.run_main(
            "tiingo-import", "--broad", "--start", "2006-01-01", "--end", "2026-09-25",
            load_api_key=lambda: "key", import_symbol=fake_import,
            import_complete=lambda start, end, symbol: symbol in ("SPY", "QQQ"))
        self.assertEqual(code, 0)
        self.assertEqual(imported, list(BROAD_UNIVERSE[2:]))
        self.assertEqual(json.loads(stdout)["symbols"]["SPY"], {"result": "ALREADY_COMPLETE"})


if __name__ == "__main__":
    unittest.main()
