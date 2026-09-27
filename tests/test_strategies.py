import hashlib
import json
import math
import unittest
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from tests.test_gld_research import sample_gld_csv
from tests.test_paper_trading import gld_csv
from tradenow.equity import EquityConfig, feature_rows, simulate_equity
from tradenow.equity_types import FeatureRow
from tradenow.gld_research import parse_gld_csv, run_gld_csv
from tradenow.paper_rules import plan_order
from tradenow.strategies import (
    GLD_CANDIDATES,
    Decision,
    Donchian,
    History,
    SmaCross,
    TimeSeriesMomentum,
    Trend200,
    VolatilityTarget,
)


# SHA-256 of outputs produced BEFORE the strategy interface existed. The
# refactored code must reproduce them exactly; a mismatch means a result changed.
GOLDEN = {
    "report-paper242": "953d1ac8fcd83e423eb12281d4749e069ea0ae91b6ce9219faa42f10d8022ea0",
    "report-sample300": "c457eafd64e37130996b28fa4a2e259ab22ea430cb6c594704afc6db6d653f55",
    "report-wavy1500": "e3dd33054d18ee9b7f1655096e114b352f1a68ee69d1b97c9bd9cb24ed682e6c",
    "sim-paper242-10-30-1.00": "c953b0a47f7013f9df05d4514115fa6b6cb2e07d9dae08f35901258a5a3802e8",
    "sim-paper242-3-10-0.50": "8a534365a0f64632fde0c8ec0e25a6a36b9b1dc09c2448dddef31259cd0e00af",
    "sim-paper242-5-20-0.25": "c71228496b8d2d89eebd9c19fc18c64873e48227bbb4b4eeaabd1f394cc913fe",
    "sim-sample300-10-30-1.00": "547935432cba376eea6642c149e7b9948b684387b944bbc6208cf6cab2dcadbe",
    "sim-sample300-3-10-0.50": "895be4bd53267d0656a42f9f09be978a3cd863eea368950f989554181dd0d133",
    "sim-sample300-5-20-0.25": "1559ff7c9bb9b9965928edcb8393b21679c126b2b79aecb57db8a56b353e95c0",
    "sim-wavy1500-10-30-1.00": "f8972d3a57e9237fcd9fe6e1db20b92704f039b0d179f6896536f25e1acdeda7",
    "sim-wavy1500-3-10-0.50": "434dea2615bfc36748045d436e7b14abb3a5bce62466b229d2e2fcf8640426eb",
    "sim-wavy1500-5-20-0.25": "2c9c58f46acbcf785030784f72c0b47b4e8667b668e4e7f1012835517ab43f87",
}


def wavy(count: int) -> bytes:
    rows = ["date,symbol,open,high,low,close,volume,adj_open,adj_high,adj_low,adj_close,"
            "adj_volume,div_cash,split_factor"]
    day, index = date(2004, 11, 18), 0
    while index < count:
        if day.weekday() < 5:
            close = Decimal(str(round(100 + index * 0.03 + 8 * math.sin(index / 40)
                                      + 3 * math.sin(index / 7), 2)))
            open_ = close - Decimal("0.2")
            rows.append(f"{day},GLD,{open_},{close + 1},{open_ - 1},{close},100000,"
                        f"{open_},{close + 1},{open_ - 1},{close},100000,0.0,1.0")
            index += 1
        day += timedelta(days=1)
    return ("\n".join(rows) + "\n").encode()


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, indent=1).encode()).hexdigest()


# The original three SMA candidates, which the golden outputs were produced with.
SMA_CANDIDATES = GLD_CANDIDATES[:3]


class RegressionTests(unittest.TestCase):
    def test_results_match_the_pre_interface_code_exactly(self):
        inputs = {"sample300": sample_gld_csv(300), "paper242": gld_csv(242),
                   "wavy1500": wavy(1500)}
        for name, data in inputs.items():
            report = run_gld_csv(data, candidates=SMA_CANDIDATES)
            # IDs change with any code edit; the gate section is derived from the
            # evaluation (hashed here) and gained fields later, tested elsewhere.
            gate = report.pop("live_gate")
            self.assertEqual(gate["research"]["candidates_evaluated"], 3)
            for key in ("run_id", "code_sha256"):
                report.pop(key)
            with self.subTest(output=f"report-{name}"):
                self.assertEqual(digest(report), GOLDEN[f"report-{name}"])
            bars = parse_gld_csv(data, EquityConfig())
            for fast, slow, fraction in ((3, 10, "0.50"), (10, 30, "1.00"), (5, 20, "0.25")):
                config = replace(EquityConfig(), fast_window=fast, slow_window=slow,
                                 max_position_fraction=Decimal(fraction))
                key = f"sim-{name}-{fast}-{slow}-{fraction}"
                with self.subTest(output=key):
                    self.assertEqual(digest(simulate_equity(bars, config)), GOLDEN[key])


class StrategyInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.bars = parse_gld_csv(sample_gld_csv(300), EquityConfig())
        self.rows = feature_rows(self.bars, SmaCross(3, 10))

    def test_history_cannot_see_the_future(self):
        history = History(self.rows, 50)
        self.assertEqual(len(history), 51)
        self.assertEqual(history[-1], self.rows[50])
        self.assertEqual(history.today, self.rows[50])
        self.assertEqual(history[-3:], self.rows[48:51])
        self.assertEqual(history[40:500], self.rows[40:51])  # slices stop at today
        for index in (51, 1000):
            with self.assertRaises(IndexError):
                history[index]
        with self.assertRaises(IndexError):
            History(self.rows, len(self.rows))

    def test_targets_must_be_between_zero_and_one(self):
        for bad in ("-0.1", "1.01", "NaN"):
            with self.assertRaises(ValueError):
                Decision(Decimal(bad))

    def test_fractional_target_buys_that_share_of_the_position(self):
        class Half:
            name, needs_features = "half", False

            def decide(self, history, holding):
                return Decision(Decimal("0.5"))

        full = simulate_equity(self.bars, EquityConfig(), SmaCross(3, 10))
        half = simulate_equity(self.bars, EquityConfig(), Half())
        bought = half["fills"][0]
        price = Decimal(bought["price"])
        self.assertEqual(bought["shares"], int(Decimal("100000") * Decimal("0.50")
                                               * Decimal("0.5") / price))
        self.assertEqual(half["signals"][0]["strategy_target"], "0.5")
        self.assertIsInstance(full["signals"][0]["strategy_target"], int)
        planned = plan_order(Decimal("0.5"), 0, Decimal("100000"), Decimal("200"),
                             Decimal("0.50"), Decimal(0))
        self.assertEqual(planned.qty, int(Decimal("25000") / Decimal("202.00")))

    def test_feature_rows_must_match_the_bars(self):
        wrong = [FeatureRow(row.date + timedelta(days=1), row.close, {}) for row in self.rows]
        with self.assertRaisesRegex(ValueError, "do not match"):
            simulate_equity(self.bars, EquityConfig(), SmaCross(3, 10), wrong)

    def test_registered_candidates_are_exactly_the_adr_010_list(self):
        self.assertEqual([item.name for item in GLD_CANDIDATES], [
            "sma_3_10", "sma_5_20", "sma_10_30", "tsmom_6_1", "tsmom_12_1", "trend_200",
            "trend_200_volfilter", "donchian_55_20", "donchian_20_10", "tsmom_12_1_vt15",
            "trend_200_vt15", "donchian_55_20_vt15"])


def today(close: str = "100", **values: str | None) -> History:
    row = FeatureRow(date(2026, 1, 5), Decimal(close),
                     {name: None if value is None else Decimal(value)
                      for name, value in values.items()})
    return History([row], 0)


class CandidateRuleTests(unittest.TestCase):
    def test_time_series_momentum(self):
        rule = TimeSeriesMomentum(12)
        self.assertEqual(rule.decide(today(mom_12_1="0.05"), False).target, 1)
        self.assertEqual(rule.decide(today(mom_12_1="0"), True).target, 0)
        self.assertIsNone(rule.decide(today(mom_12_1=None), False))
        with self.assertRaises(ValueError):
            TimeSeriesMomentum(3)

    def test_trend_with_and_without_volatility_filter(self):
        calm = today(dist_sma_200="0.02", vol_20="0.10", vol_20_median_252="0.15")
        stormy = today(dist_sma_200="0.02", vol_20="0.20", vol_20_median_252="0.15")
        self.assertEqual(Trend200(False).decide(stormy, False).target, 1)
        self.assertEqual(Trend200(True).decide(calm, False).target, 1)
        self.assertEqual(Trend200(True).decide(stormy, False).target, 0)
        below = today(dist_sma_200="-0.01", vol_20="0.10", vol_20_median_252="0.15")
        self.assertEqual(Trend200(True).decide(below, True).target, 0)

    def test_donchian_enters_on_breakout_and_holds_until_breakdown(self):
        rule = Donchian(55, 20)
        channel = {"donchian_high_55": "110", "donchian_low_20": "95"}
        self.assertEqual(rule.decide(today("111", **channel), False).target, 1)
        self.assertEqual(rule.decide(today("105", **channel), False).target, 0)
        self.assertEqual(rule.decide(today("105", **channel), True).target, 1)
        self.assertEqual(rule.decide(today("94", **channel), True).target, 0)
        with self.assertRaises(ValueError):
            Donchian(30, 10)

    def test_volatility_target_scales_entries_down_never_up(self):
        rule = VolatilityTarget(TimeSeriesMomentum(12))
        volatile = rule.decide(today(mom_12_1="0.1", vol_20="0.30"), False)
        self.assertEqual(volatile.target, Decimal("0.5000"))
        calm = rule.decide(today(mom_12_1="0.1", vol_20="0.05"), False)
        self.assertEqual(calm.target, Decimal("1.0000"))
        out = rule.decide(today(mom_12_1="-0.1", vol_20="0.30"), False)
        self.assertEqual(out.target, 0)
        self.assertEqual(rule.name, "tsmom_12_1_vt15")

    def test_every_candidate_runs_through_the_full_evaluation(self):
        report = run_gld_csv(wavy(1500))
        for hypothesis in report["research"]["hypotheses"]:
            with self.subTest(candidate=hypothesis["name"]):
                self.assertTrue(hypothesis["validation"]["signals"])
        self.assertEqual(report["live_gate"]["research"]["candidates_evaluated"], 12)


if __name__ == "__main__":
    unittest.main()
