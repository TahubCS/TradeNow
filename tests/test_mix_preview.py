import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from test_multi import synthetic_universe

from tradenow.__main__ import main
from tradenow.logs import close_logging
from tradenow.mix_config import mix_from_targets, parse_targets_option
from tradenow.mix_preview import (
    PREVIEW_CONFIG,
    _worst_drawdown,
    _years,
    preview,
    quarterly_schedule,
)
from tradenow.portfolio import simulate_portfolio


D = Decimal


def weekdays(start: date, count: int) -> list[date]:
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


class ScheduleTests(unittest.TestCase):
    def test_first_bar_and_quarter_ends(self):
        days = weekdays(date(2026, 1, 5), 140)  # into July
        schedule = quarterly_schedule(days)
        self.assertEqual([days[index] for index in schedule],
                         [date(2026, 1, 5), date(2026, 3, 31), date(2026, 6, 30)])


class PreviewTests(unittest.TestCase):
    def test_trades_only_after_scheduled_checks(self):
        universe = synthetic_universe(800, symbols=("SPY", "AGG"), drifts=(3, 0))
        report = preview(universe, mix_from_targets({"SPY": D("0.6"), "AGG": D("0.4")}))
        self.assertTrue(report["information_only"])
        self.assertIn("spy_alone", report)
        days = universe.dates
        allowed = {days[index + 1].isoformat() for index in quarterly_schedule(days)}
        self.assertEqual(json.loads(json.dumps(report)), report)  # plain JSON
        # The same run's fills fall only on the opens after scheduled checks.
        result = simulate_portfolio(universe, lambda _views: {"SPY": D("0.6"),
                                                                "AGG": D("0.4")},
                                    PREVIEW_CONFIG, schedule=quarterly_schedule(days))
        self.assertTrue({fill["date"] for fill in result["fills"]} <= allowed)
        self.assertEqual(report["mix_result"]["fills"], len(result["fills"]))

    def test_needs_history_for_every_symbol(self):
        universe = synthetic_universe(800, symbols=("SPY", "AGG"))
        with self.assertRaises(ValueError):
            preview(universe, mix_from_targets({"TLT": D("0.5")}))

    def test_worst_drawdown_and_years_by_hand(self):
        curve = [{"date": day, "equity": equity} for day, equity in (
            ("2025-12-30", "100"), ("2025-12-31", "120"), ("2026-01-02", "90"),
            ("2026-01-05", "130"), ("2026-01-06", "125"))]
        self.assertEqual(_worst_drawdown(curve, D(100)),
                         {"pct": "25.000", "peak_date": "2025-12-31",
                          "trough_date": "2026-01-02", "recovered_date": "2026-01-05"})
        self.assertEqual(_years(curve, D(100)), {"2025": "20.00", "2026": "4.17"})


class TargetsOptionTests(unittest.TestCase):
    def test_parses_and_checks_like_the_file(self):
        mix = parse_targets_option("spy=0.6, AGG=0.40")
        self.assertEqual(mix.targets, {"SPY": D("0.6"), "AGG": D("0.40")})
        self.assertEqual(mix.sha256, mix_from_targets({"SPY": D("0.60"), "AGG": D("0.4")}).sha256)
        for text in ("SPY", "SPY=0.6,SPY=0.2", "SPY=0.7,AGG=0.4", "SPY=x"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_targets_option(text)


class CommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(close_logging)
        self.root = Path(directory.name)

    def run_main(self, *argv: str, universe=None) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {"TRADENOW_DATA_DIR": str(self.root)}), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            if universe is None:
                code = main(list(argv))
            else:
                with patch("tradenow.__main__.load_universe", return_value=universe):
                    code = main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_preview_writes_a_readable_report(self):
        universe = synthetic_universe(800, symbols=("SPY", "AGG"))
        code, stdout, _ = self.run_main("mix-preview", "--targets", "SPY=0.6,AGG=0.4",
                                        "--output", str(self.root / "mix"), universe=universe)
        self.assertEqual(code, 0)
        printed = json.loads(stdout)
        self.assertEqual(printed["mix"], {"SPY": "0.6", "AGG": "0.4"})
        self.assertTrue(Path(printed["readable_report"]).exists())

    def test_missing_imports_explain_how_to_fetch_them(self):
        code, _, stderr = self.run_main("mix-preview", "--targets", "SPY=0.6,AGG=0.4")
        self.assertEqual(code, 1)
        self.assertIn("tiingo-import --symbols SPY,AGG", stderr)


if __name__ == "__main__":
    unittest.main()
