import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from tradenow.alpaca_paper import BrokerOrder, Position
from tradenow.mix_config import load_mix, parse_mix
from tradenow.mix_rules import (
    MixOrderRecord,
    apply_broker,
    drawdown_alerts,
    plan_rebalance,
    quarter,
    rebalance_due,
    reconcile_mix,
)


D = Decimal
BUFFER = D("0.01")


def actions(plan) -> list[tuple]:
    return [(item.symbol, item.side, item.qty, item.limit_price, item.reason)
            for item in plan.actions]


class MixConfigTests(unittest.TestCase):
    def test_valid_mix_keeps_order_and_cash(self):
        targets = parse_mix({"targets": {"SPY": 0.6, "AGG": "0.35"}})
        self.assertEqual(targets, {"SPY": D("0.6"), "AGG": D("0.35")})
        self.assertEqual(list(targets), ["SPY", "AGG"])

    def test_invalid_mixes_are_refused(self):
        for data in ({"targets": {"SPY": 0.6}, "extra": 1}, {"targets": {}},
                     {"targets": {"spy": 0.5}}, {"targets": {"SPY": 0}},
                     {"targets": {"SPY": 1.5}}, {"targets": {"SPY": "0.12345"}},
                     {"targets": {"SPY": 0.6, "AGG": 0.5}}, {"targets": {"SPY": True}},
                     {"targets": {f"A{chr(65 + i)}": 0.05 for i in range(13)}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_mix(data)

    def test_file_loading_and_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mix.toml"
            with self.assertRaisesRegex(ValueError, "No mix file"):
                load_mix(path)
            path.write_text("[targets]\nSPY = 0.60\nAGG = 0.40\n", encoding="utf-8")
            first = load_mix(path)
            path.write_text("# comment\n[targets]\nSPY = 0.6\nAGG = 0.4\n", encoding="utf-8")
            self.assertEqual(load_mix(path).sha256, first.sha256)  # same weights
            path.write_text("[targets]\nSPY = 0.5\nAGG = 0.4\n", encoding="utf-8")
            changed = load_mix(path)
            self.assertNotEqual(changed.sha256, first.sha256)
            self.assertEqual(changed.cash_weight, D("0.1"))
            path.write_text("[targets\nSPY = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not parse"):
                load_mix(path)


class PlanTests(unittest.TestCase):
    TARGETS = {"SPY": D("0.6"), "AGG": D("0.4")}

    def test_first_investment_buys_within_cash(self):
        plan = plan_rebalance(self.TARGETS, {}, {"SPY": D(500), "AGG": D(100)}, D(10000), BUFFER)
        # floor(6000 / 505) = 11 and floor(4000 / 101) = 39; 9,494 at the limits.
        self.assertEqual((plan.phase, actions(plan)),
                         ("BUY", [("SPY", "buy", 11, D("505.00"), "BELOW_BAND"),
                                  ("AGG", "buy", 39, D("101.00"), "BELOW_BAND")]))

    def test_inside_the_band_nothing_trades(self):
        plan = plan_rebalance(self.TARGETS, {"SPY": 12, "AGG": 40},
                              {"SPY": D(500), "AGG": D(100)}, D(0), BUFFER)
        self.assertEqual((plan.phase, plan.actions), ("NONE", ()))
        self.assertEqual(plan.drift, {"SPY": D(0), "AGG": D(0)})

    def test_sells_come_first_then_buys_from_actual_cash(self):
        # SPY rallied to 67.7% of 12,400: above its band by 7.7 points.
        sell = plan_rebalance(self.TARGETS, {"SPY": 12, "AGG": 40},
                              {"SPY": D(700), "AGG": D(100)}, D(0), BUFFER)
        self.assertEqual((sell.phase, actions(sell)),
                         ("SELL", [("SPY", "sell", 2, None, "ABOVE_BAND")]))  # keep floor(10.6)
        # A later evening: the sale left 1,400 cash; AGG is 7.7 points under.
        buy = plan_rebalance(self.TARGETS, {"SPY": 10, "AGG": 40},
                             {"SPY": D(700), "AGG": D(100)}, D(1400), BUFFER)
        self.assertEqual(actions(buy), [("AGG", "buy", 9, D("101.00"), "BELOW_BAND")])

    def test_buys_shrink_to_cash(self):
        # A (within band above target) holds 100 of 185; B wants floor(92.5 / 10.10) = 9.
        plan = plan_rebalance({"A": D("0.5"), "B": D("0.5")}, {"A": 10},
                              {"A": D(10), "B": D(10)}, D(85), BUFFER)
        self.assertEqual(actions(plan), [("B", "buy", 8, D("10.10"), "SHRUNK_TO_CASH")])

    def test_symbols_outside_the_mix_are_sold_whatever_their_size(self):
        plan = plan_rebalance({"SPY": D("0.9")}, {"SPY": 18, "XYZ": 1},
                              {"SPY": D(500), "XYZ": D(10)}, D(1000), BUFFER)
        self.assertEqual(actions(plan), [("XYZ", "sell", 1, None, "NOT_IN_MIX")])

    def test_unaffordable_buys_end_the_rebalance(self):
        plan = plan_rebalance({"A": D(1)}, {}, {"A": D(1000)}, D(500), BUFFER)
        self.assertEqual(plan.phase, "NONE")
        self.assertIn("cannot afford", plan.note)

    def test_refuses_margin_and_missing_prices(self):
        with self.assertRaises(ValueError):
            plan_rebalance(self.TARGETS, {}, {"SPY": D(500), "AGG": D(100)}, D(-1), BUFFER)
        with self.assertRaises(ValueError):
            plan_rebalance(self.TARGETS, {}, {"SPY": D(500)}, D(100), BUFFER)


class TimingAndAlertTests(unittest.TestCase):
    def test_quarterly_timing(self):
        self.assertEqual((quarter(date(2026, 3, 31)), quarter(date(2026, 4, 1))),
                         ("2026Q1", "2026Q2"))
        day = date(2026, 9, 25)
        self.assertTrue(rebalance_due(None, None, False, "mix", day))  # first run
        self.assertFalse(rebalance_due("2026Q3", "mix", False, "mix", day))
        self.assertTrue(rebalance_due("2026Q2", "mix", False, "mix", day))
        self.assertTrue(rebalance_due("2026Q3", "old", False, "mix", day))  # mix changed
        self.assertTrue(rebalance_due("2026Q3", "mix", True, "mix", day))  # unfinished

    def test_drawdown_levels_alert_once_per_peak(self):
        state = drawdown_alerts(None, D(0), D(100))
        self.assertEqual((state.peak, state.crossed), (D(100), ()))
        state = drawdown_alerts(state.peak, state.alerted, D(89))
        self.assertEqual(state.crossed, (D("0.10"),))
        state = drawdown_alerts(state.peak, state.alerted, D(85))
        self.assertEqual(state.crossed, ())
        state = drawdown_alerts(state.peak, state.alerted, D(75))
        self.assertEqual(state.crossed, (D("0.20"),))
        state = drawdown_alerts(state.peak, state.alerted, D(110))  # a new peak starts over
        self.assertEqual((state.peak, state.alerted), (D(110), D(0)))
        state = drawdown_alerts(state.peak, state.alerted, D(60))
        self.assertEqual(state.crossed, (D("0.10"), D("0.20"), D("0.30")))


def record(symbol: str, side: str, qty: int, filled: int, status: str = "filled",
           client_id: str = "tn-mix-a") -> MixOrderRecord:
    return MixOrderRecord(client_id, "p", date(2026, 9, 28), symbol, side, qty, None, status,
                          D(100), filled_qty=filled)


def broker_order(client_id: str, symbol: str = "SPY", side: str = "buy",
                 qty: int = 5) -> BrokerOrder:
    return BrokerOrder("b1", client_id, symbol, side, qty, 0, None, "new")


class ReconcileTests(unittest.TestCase):
    def test_matching_ledger_and_account(self):
        ledger = [record("SPY", "buy", 10, 10, client_id="tn-mix-1"),
                  record("SPY", "sell", 3, 3, client_id="tn-mix-2"),
                  record("AGG", "buy", 5, 5, client_id="tn-mix-3")]
        result = reconcile_mix(ledger, [Position("SPY", 7), Position("AGG", 5)], [])
        self.assertTrue(result.ok)
        self.assertEqual(result.expected, {"SPY": 7, "AGG": 5})

    def test_every_difference_is_a_problem(self):
        ledger = [record("SPY", "buy", 10, 10, client_id="tn-mix-1"),
                  record("AGG", "buy", 5, 0, status="submitting", client_id="tn-mix-2"),
                  record("IEF", "buy", 5, 0, status="new", client_id="tn-mix-3")]
        result = reconcile_mix(ledger, [Position("SPY", 9), Position("GLD", 1)],
                               [broker_order("tn-other")])
        kinds = sorted(problem.split(":")[0] for problem in result.problems)
        self.assertEqual(kinds, ["LEDGER_ORDER_NOT_OPEN", "POSITION_MISMATCH",
                                 "UNEXPECTED_POSITION", "UNKNOWN_OPEN_ORDER",
                                 "UNRESOLVED_SUBMISSION"])

    def test_broker_updates_must_match_the_record(self):
        mine = record("SPY", "buy", 5, 0, status="new", client_id="tn-mix-1")
        with self.assertRaises(ValueError):
            apply_broker(mine, broker_order("tn-mix-1", symbol="AGG"), definitive=True)
        self.assertEqual(apply_broker(mine, None, definitive=True).status, "not_found")
        self.assertEqual(apply_broker(mine, None, definitive=False).status, "new")


if __name__ == "__main__":
    unittest.main()
