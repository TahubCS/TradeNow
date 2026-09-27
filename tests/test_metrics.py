import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from tests.test_gld_research import sample_gld_csv
from tradenow.experiments import code_commit, read_experiments, record_experiment
from tradenow.gld_research import render_gld_markdown, run_gld_csv
from tradenow.metrics import daily_returns, max_drawdown, performance, trade_statistics
from tradenow.strategies import GLD_CANDIDATES


START = Decimal("100")
FIRST, LAST = date(2025, 1, 2), date(2026, 1, 2)


class MetricTests(unittest.TestCase):
    def test_returns_and_drawdown_by_hand(self):
        equity = [Decimal("110"), Decimal("99"), Decimal("121")]
        self.assertEqual(daily_returns(START, equity),
                         [Decimal("0.1"), Decimal("-0.1"), Decimal("121") / 99 - 1])
        self.assertEqual(max_drawdown(START, equity), Decimal("0.1"))

    def test_steady_growth_has_no_volatility_ratio(self):
        result = performance(START, [Decimal("101"), Decimal("102.01")], FIRST, LAST)
        self.assertEqual(result["total_return_pct"], "2.010")
        self.assertEqual(result["annualized_volatility_pct"], "0.000")
        self.assertIsNone(result["sharpe"])  # zero volatility: undefined, not infinite
        self.assertIsNone(result["sortino"])  # no down days
        self.assertIsNone(result["calmar"])  # no drawdown

    def test_sharpe_and_sortino_by_hand(self):
        # Returns +10%, -10%: mean 0, so both ratios are exactly zero.
        result = performance(START, [Decimal("110"), Decimal("99")], FIRST, LAST)
        self.assertEqual(result["sharpe"], "0.00")
        self.assertEqual(result["sortino"], "0.00")
        # Sample std of (+0.1, -0.1) is sqrt(0.02); annualized = sqrt(0.02 * 252).
        expected = (Decimal("0.02") * 252).sqrt() * 100
        self.assertEqual(result["annualized_volatility_pct"],
                         str(expected.quantize(Decimal("0.001"))))
        self.assertEqual(result["max_drawdown_pct"], "10.000")

    def test_exposure_and_trade_statistics(self):
        trades = [{"entry_date": "2025-01-02", "exit_date": "2025-01-12", "net_pnl": "30"},
                  {"entry_date": "2025-02-01", "exit_date": "2025-02-21", "net_pnl": "-10"},
                  {"entry_date": "2025-03-01", "exit_date": "2025-03-04", "net_pnl": "-5"}]
        stats = trade_statistics(trades)
        self.assertEqual(stats, {"closed_trades": 3, "hit_rate_pct": "33.333",
                                 "profit_factor": "2.00", "average_holding_days": "11.0"})
        self.assertIsNone(trade_statistics(trades[:1])["profit_factor"])  # no losses
        result = performance(START, [Decimal("101")] * 4, FIRST, LAST, invested_days=1)
        self.assertEqual(result["exposure_pct"], "25.000")

    def test_invalid_curves_are_refused(self):
        with self.assertRaises(ValueError):
            performance(START, [], FIRST, LAST)
        with self.assertRaises(ValueError):
            performance(START, [Decimal("0")], FIRST, LAST)


class ReportAndExperimentTests(unittest.TestCase):
    def test_same_inputs_give_byte_identical_reports(self):
        # Phase 4's exit condition: identical data and settings, identical results.
        first = json.dumps(run_gld_csv(sample_gld_csv()), sort_keys=True)
        second = json.dumps(run_gld_csv(sample_gld_csv()), sort_keys=True)
        self.assertEqual(first, second)

    def test_report_compares_risk_adjusted_metrics(self):
        report = run_gld_csv(sample_gld_csv())
        holdout = report["evaluation"]["holdout"]
        for key in ("strategy", "buy_hold_50pct", "buy_hold_100pct"):
            self.assertIn("sharpe", holdout[key]["metrics"])
        self.assertEqual(holdout["buy_hold_100pct"]["metrics"]["exposure_pct"], "100.000")
        markdown = render_gld_markdown(report)
        self.assertIn("| Portfolio | Volatility | Sharpe | Sortino | Calmar | Exposure |",
                      markdown)
        self.assertNotIn("None", markdown)

    def test_each_distinct_experiment_is_logged_once(self):
        report = run_gld_csv(sample_gld_csv())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiments.jsonl"
            first = record_experiment(report, path)
            again = record_experiment(report, path)
            self.assertEqual((first["recorded"], again["recorded"]), (True, False))
            other = run_gld_csv(sample_gld_csv(300))
            self.assertEqual(record_experiment(other, path)["distinct_experiments"], 2)
            records = read_experiments(path)
        self.assertEqual(len(records), 2)
        record = records[0]
        self.assertEqual(record["experiment_id"], report["run_id"])
        self.assertEqual(record["candidates"], [item.name for item in GLD_CANDIDATES])
        self.assertEqual(record["data"]["sha256"], report["data"]["sha256"])
        self.assertIn(record["results"]["live_gate"]["verdict"], ("PASS", "FAIL"))

    def test_code_commit_reads_loose_and_packed_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git" / "refs" / "heads").mkdir(parents=True)
            (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (root / ".git" / "packed-refs").write_text(
                "# pack-refs\nabc123 refs/heads/main\n", encoding="utf-8")
            self.assertEqual(code_commit(root), "abc123")
            (root / ".git" / "refs" / "heads" / "main").write_text("def456\n", encoding="utf-8")
            self.assertEqual(code_commit(root), "def456")
            self.assertIsNone(code_commit(root / "missing"))


if __name__ == "__main__":
    unittest.main()
