import unittest
from datetime import date, timedelta
from decimal import Decimal

from tradenow.live_gate import (
    FAIL,
    MIN_FORWARD_SESSIONS,
    PASS,
    EquitySnapshot,
    forward_gate,
    forward_run,
    research_gate,
    strategy_version,
    verdict,
)


def evaluation(beat: int = 20, windows: int = 29, trades: int = 40,
               strategy: str = "120.000", hold: str = "90.000",
               stressed: str = "110.000") -> dict:
    return {"rolling_pre_holdout": {"summary": {
                "windows": windows, "beat_buy_hold_50pct_windows": beat,
                "closed_trades": trades, "compounded_return_pct": strategy,
                "compounded_buy_hold_100pct_return_pct": hold}},
            "rolling_pre_holdout_stressed": {"slippage_per_share": "0.10", "summary": {
                "compounded_return_pct": stressed,
                "compounded_buy_hold_100pct_return_pct": "88.000"}}}


def history(sessions: int, growth: str, version: str = "v1",
            start: date = date(2026, 1, 5)) -> tuple[list[EquitySnapshot], dict]:
    snapshots, closes, equity, price = [], {}, Decimal("100000"), Decimal("300")
    for index in range(sessions):
        day = start + timedelta(days=index)
        snapshots.append(EquitySnapshot(day, equity, version))
        closes[day] = price
        equity *= 1 + Decimal(growth)
        price *= Decimal("1.0001")
    return snapshots, closes


GOOD_EXECUTION = {"outcomes": {"FILLED": 12}, "mean_slippage_bps": {"simulated": "4.00"}}


class LiveGateTests(unittest.TestCase):
    def test_research_stage_passes_only_with_every_check(self):
        self.assertTrue(research_gate(evaluation())["passed"])
        failing = {
            "R1_BEATS_FULL_BUY_AND_HOLD": evaluation(strategy="80.000"),
            "R2_BEATS_CONSISTENTLY": evaluation(beat=17),
            "R3_ENOUGH_TRADES": evaluation(trades=29),
            "R4_SURVIVES_COSTS": evaluation(stressed="85.000"),
        }
        for name, item in failing.items():
            with self.subTest(name=name):
                result = research_gate(item)
                self.assertFalse(result["passed"])
                self.assertEqual([check["check"] for check in result["checks"]
                                  if not check["passed"]], [name])

    def test_current_gld_strategy_numbers_fail(self):
        # The report's own figures: 10 of 29 windows beat same-exposure buy-and-hold.
        self.assertFalse(research_gate(evaluation(beat=10))["passed"])

    def test_forward_stage_needs_a_year_of_beating_buy_and_hold(self):
        snapshots, closes = history(MIN_FORWARD_SESSIONS, "0.0005")
        self.assertTrue(forward_gate(snapshots, closes, GOOD_EXECUTION)["passed"])
        short, short_closes = history(MIN_FORWARD_SESSIONS - 1, "0.0005")
        self.assertFalse(forward_gate(short, short_closes, GOOD_EXECUTION)["passed"])
        lagging, lagging_closes = history(MIN_FORWARD_SESSIONS, "0.00005")
        result = forward_gate(lagging, lagging_closes, GOOD_EXECUTION)
        self.assertIn("F2_BEATS_FULL_BUY_AND_HOLD",
                      [item["check"] for item in result["checks"] if not item["passed"]])

    def test_changing_the_strategy_restarts_the_forward_count(self):
        old, closes = history(300, "0.0005", "v1")
        new, new_closes = history(5, "0.0005", "v2", start=old[-1].date + timedelta(days=1))
        run = forward_run(old + new)
        self.assertEqual({item.strategy_version for item in run}, {"v2"})
        self.assertEqual(len(run), 5)
        self.assertFalse(forward_gate(old + new, {**closes, **new_closes},
                                      GOOD_EXECUTION)["passed"])

    def test_costs_must_match_the_simulation(self):
        snapshots, closes = history(MIN_FORWARD_SESSIONS, "0.0005")
        for execution in ({"outcomes": {"FILLED": 3},
                           "mean_slippage_bps": {"simulated": "1.00"}},
                          {"outcomes": {"FILLED": 12},
                           "mean_slippage_bps": {"simulated": "25.00"}}):
            self.assertFalse(forward_gate(snapshots, closes, execution)["passed"])

    def test_verdict_needs_both_stages(self):
        research = research_gate(evaluation())
        snapshots, closes = history(MIN_FORWARD_SESSIONS, "0.0005")
        forward = forward_gate(snapshots, closes, GOOD_EXECUTION)
        self.assertEqual(verdict(research, forward)["verdict"], PASS)
        self.assertEqual(verdict(research, None)["verdict"], FAIL)
        failed = verdict(research_gate(evaluation(beat=10)), forward)
        self.assertEqual(failed["verdict"], FAIL)
        self.assertEqual(failed["failing_checks"], ["R2_BEATS_CONSISTENTLY"])
        self.assertIn("Do not trade real money", failed["meaning"])

    def test_strategy_version_depends_on_code_rule_and_risk(self):
        base = strategy_version("code", "sma_3_10", "risk")
        self.assertEqual(base, strategy_version("code", "sma_3_10", "risk"))
        self.assertNotEqual(base, strategy_version("code2", "sma_3_10", "risk"))
        self.assertNotEqual(base, strategy_version("code", None, "risk"))
        self.assertNotEqual(base, strategy_version("code", "sma_3_10", "risk2"))


if __name__ == "__main__":
    unittest.main()
