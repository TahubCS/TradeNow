import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal

from tradenow.equity import EquityBar, EquityConfig
from tradenow.gld_evaluation import _buy_and_hold
from tradenow.gld_research import run_gld_csv
from tests.test_gld_research import sample_gld_csv


class GldEvaluationTests(unittest.TestCase):
    def test_buy_and_hold_respects_cash_and_exposure(self):
        bars = [EquityBar(date(2025, 1, 1), "GLD", Decimal(10), Decimal(10),
                          Decimal(10), Decimal(10), 100),
                EquityBar(date(2026, 1, 1), "GLD", Decimal(12), Decimal(12),
                          Decimal(12), Decimal(12), 100)]
        config = EquityConfig(starting_cash=Decimal(100),
                              slippage_per_share=Decimal(0),
                              commission_per_order=Decimal(0))
        half = _buy_and_hold(bars, config, Decimal("0.50"))
        full = _buy_and_hold(bars, config, Decimal(1))
        self.assertEqual((half["shares"], half["ending_equity"],
                          half["total_return_pct"]), (5, "110", "10.000"))
        self.assertEqual((full["shares"], full["ending_equity"],
                          full["total_return_pct"]), (10, "120", "20.000"))
        with self.assertRaisesRegex(ValueError, "tradable first test bar"):
            _buy_and_hold([replace(bars[0], volume=0), bars[1]], config, Decimal("0.50"))

    def test_rolling_tests_do_not_read_final_holdout(self):
        source = sample_gld_csv(1200)
        original = run_gld_csv(source)
        rows = source.splitlines()
        fields = rows[-1].decode().split(",")
        for index, value in ((2, "500"), (3, "501"), (4, "499"), (5, "500"),
                             (7, "500"), (8, "501"), (9, "499"), (10, "500")):
            fields[index] = value
        rows[-1] = ",".join(fields).encode()
        changed = run_gld_csv(b"\n".join(rows) + b"\n")
        self.assertNotEqual(original["research"]["holdout_summary"],
                            changed["research"]["holdout_summary"])
        self.assertEqual(original["evaluation"]["rolling_pre_holdout"],
                         changed["evaluation"]["rolling_pre_holdout"])

    def test_slippage_scenarios_keep_the_selected_hypothesis(self):
        report = run_gld_csv(sample_gld_csv())
        scenarios = report["evaluation"]["slippage_sensitivity"]
        self.assertEqual([item["slippage_per_share"] for item in scenarios],
                         ["0.01", "0.05", "0.10"])
        self.assertEqual(scenarios[0]["total_return_pct"],
                         report["research"]["holdout_summary"]["total_return_pct"])
        self.assertTrue(all(item["selected_hypothesis"] ==
                            report["research"]["selected_hypothesis"]
                            for item in scenarios))
        self.assertEqual(report["evaluation"]["holdout"]["cash"],
                         {"total_return_pct": "0.000",
                          "annualized_return_pct": "0.000",
                          "max_drawdown_pct": "0.000"})


if __name__ == "__main__":
    unittest.main()
