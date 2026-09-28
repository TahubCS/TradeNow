import io
import json
import random
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from test_multi import synthetic_universe

from tradenow.__main__ import main
from tradenow.equity_types import EquityBar
from tradenow.gld_evaluation import _rolling_checks
from tradenow.logs import close_logging
from tradenow.multi_evaluation import rolling_checks, universe_rows
from tradenow.multi_strategies import MULTI_CANDIDATES
from tradenow.portfolio import PortfolioConfig
from tradenow.risk_review import (
    Curve,
    NotReproduced,
    build_report,
    chain,
    compare,
    credited,
    render_markdown,
    review_gld_study,
    review_portfolio_study,
    risk_matched,
    statistics,
    volatility_ratio,
)
from tradenow.selection import RESEARCH_CONFIG, chronological_split, history_rows
from tradenow.strategies import GLD_CANDIDATES


D = Decimal
DAYS = [date(2026, 1, 5) + timedelta(days=offset) for offset in range(4)]


def run(start: str, *days: tuple[date, str, str]) -> dict:
    return {"starting_cash": start, "equity_curve": [
        {"date": day.isoformat(), "equity": equity, "cash": cash} for day, equity, cash in days]}


def curve(returns: list[str]) -> Curve:
    return Curve(DAYS[:len(returns)], [D(value) for value in returns],
                 [D(1)] * len(returns))


class CurveTests(unittest.TestCase):
    def test_runs_chain_with_cash_shares_from_the_previous_close(self):
        chained = chain([run("100", (DAYS[0], "110", "10"), (DAYS[1], "99", "9")),
                         run("100", (DAYS[2], "100", "100"), (DAYS[3], "120", "20"))])
        self.assertEqual(chained.returns, [D("0.1"), D("-0.1"), D(0), D("0.2")])
        # Each run starts all in cash; then the share is cash / equity at the close.
        self.assertEqual(chained.cash_before, [D(1), D(10) / D(110), D(1), D(1)])

    def test_idle_cash_is_credited(self):
        chained = Curve(DAYS[:2], [D("0.1"), D("-0.1")], [D(1), D("0.5")])
        self.assertEqual(credited(chained, [D("0.01"), D("0.02")]), [D("0.11"), D("-0.09")])


class StatisticsTests(unittest.TestCase):
    def test_hand_checked_measures(self):
        result = statistics([D("0.01"), D("-0.01"), D("0.02"), D(0)], [D(0)] * 4)
        # Mean 0.005, sample deviation sqrt(0.0005 / 3), 252 trading days.
        self.assertEqual(result["total_return_pct"], "1.990")  # 1.01 * 0.99 * 1.02
        self.assertEqual(result["annualized_volatility_pct"], "20.494")
        self.assertEqual(result["sharpe"], "6.15")
        self.assertEqual(result["sortino"], "15.87")  # downside sqrt(0.0001 / 4)
        self.assertEqual(result["max_drawdown_pct"], "1.000")  # 1.01 -> 0.9999

    def test_sharpe_measures_returns_above_cash(self):
        returns = [D("0.01"), D("-0.01"), D("0.02"), D(0)]
        # Cash at the same rate every day shifts the mean, not the deviation.
        above = statistics(returns, [D("0.001")] * 4)
        self.assertEqual(above["sharpe"], "4.92")  # 0.004 / deviation * sqrt(252)
        self.assertIsNone(statistics([D("0.01")] * 3, [D("0.01")] * 3)["sharpe"])


class RiskMatchTests(unittest.TestCase):
    def test_volatility_ratio_and_matched_benchmark(self):
        process = [D("0.01"), D("-0.01"), D("0.02"), D(0)]
        benchmark = [value * 2 for value in process]
        self.assertEqual(volatility_ratio(process, benchmark).quantize(D("0.0001")), D("0.5"))
        self.assertEqual(risk_matched(benchmark, [D("0.001")] * 4, D("0.5")),
                         [D("0.0105"), D("-0.0095"), D("0.0205"), D("0.0005")])

    def test_process_equal_to_its_risk_match_does_not_beat_it(self):
        process = curve(["0.01", "-0.01", "0.02", "0"])
        benchmark = curve(["0.02", "-0.02", "0.04", "0"])
        rates = dict.fromkeys(DAYS, D(0))
        result = compare(process, {"b1": benchmark}, rates)
        self.assertEqual(result["volatility_ratio_k"], {"b1": "0.5000"})
        self.assertEqual(result["answers"]["cash_0pct"]["b1"],
                         {"beats_risk_matched_return": False, "higher_sharpe": False})
        self.assertEqual(result["measures"]["cash_0pct"]["process"]["total_return_pct"],
                         result["measures"]["cash_0pct"]["b1_risk_matched"]["total_return_pct"])

    def test_missing_cash_rates_stop_the_review(self):
        with self.assertRaises(ValueError):
            compare(curve(["0.01", "0.02"]), {"b1": curve(["0.02", "0.01"])}, {DAYS[0]: D(0)})


class ReproductionTests(unittest.TestCase):
    def test_portfolio_study_must_match_its_logged_summary(self):
        universe = synthetic_universe(1000)
        logged = rolling_checks(universe, universe_rows(universe), PortfolioConfig(),
                                800, MULTI_CANDIDATES)["summary"]
        rates = dict.fromkeys(universe.dates, D(0))
        record = {"experiment_id": "abc", "results": {"rolling": logged}}
        review = review_portfolio_study("ADR-011", universe, MULTI_CANDIDATES, record, rates)
        self.assertTrue(review["reproduced"])
        self.assertEqual(set(review["answers"]["cash_shy"]), {"b1", "b2"})
        tampered = {"experiment_id": "abc",
                    "results": {"rolling": {**logged, "closed_trades": logged["closed_trades"] + 1}}}
        with self.assertRaises(NotReproduced):
            review_portfolio_study("ADR-011", universe, MULTI_CANDIDATES, tampered, rates)

    def test_rebuilt_gld_windows_match_the_gld_evaluation(self):
        rng = random.Random(3)
        bars, day, price = [], date(2010, 1, 4), D(100)
        while len(bars) < 1000:
            if day.weekday() < 5:
                open_ = price
                price = (price * (1000 + rng.randint(-12, 13)) / 1000).quantize(D("0.01"))
                bars.append(EquityBar(day, "GLD", open_, max(open_, price) + 1,
                                      min(open_, price) - 1, price, 5000))
            day += timedelta(days=1)
        periods = chronological_split(bars)
        pre = periods["development"] + periods["validation"]
        rows = history_rows(bars, GLD_CANDIDATES)
        assert rows is not None
        logged = _rolling_checks(pre, RESEARCH_CONFIG, GLD_CANDIDATES, rows[:len(pre)])["summary"]
        rates = {bar.date: D(0) for bar in bars}
        review = review_gld_study(bars, {"experiment_id": "gld", "results": {"rolling": logged}},
                                  rates)
        self.assertTrue(review["reproduced"])
        self.assertEqual(set(review["answers"]["cash_0pct"]), {"gld"})


class ReportTests(unittest.TestCase):
    def test_report_is_information_only_and_deterministic(self):
        study = {"study": "ADR-011", "experiment_id": "abc", "reproduced": True,
                 **compare(curve(["0.01", "-0.01", "0.02"]),
                           {"b1": curve(["0.02", "-0.01", "0.01"])},
                           dict.fromkeys(DAYS, D("0.0001")))}
        report = build_report([study], {"symbol": "SHY", "sha256": "f" * 64})
        self.assertTrue(report["information_only"])
        again = build_report([study], {"symbol": "SHY", "sha256": "f" * 64})
        self.assertEqual(json.dumps(report), json.dumps(again))
        self.assertIn("information only", render_markdown(report))

    def test_command_fails_clearly_without_imports(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(close_logging)
        stderr = io.StringIO()
        with patch.dict("os.environ", {"TRADENOW_DATA_DIR": directory.name}), \
                redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            self.assertEqual(main(["risk-report", "--output",
                                   str(Path(directory.name) / "risk")]), 1)
        self.assertIn("Error", stderr.getvalue())
        self.assertFalse((Path(directory.name) / "experiments.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
