import unittest
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from tradenow.equity import EquityBar, EquityConfig, simulate_equity


def gld_bars(prices: list[int]) -> list[EquityBar]:
    bars = []
    day = date(2026, 1, 5)
    for value in prices:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        price = Decimal(value)
        bars.append(EquityBar(day, "GLD", price, price + 1, price - 1, price, 100))
        day += timedelta(days=1)
    return bars


class EquityTests(unittest.TestCase):
    def test_whole_share_round_trip_debits_cash_and_marks_equity(self):
        bars = gld_bars([10, 11, 12, 13, 14, 12, 10, 9, 8])
        config = EquityConfig(starting_cash=Decimal("100"),
                              commission_per_order=Decimal("1"),
                              fast_window=2, slow_window=3)
        result = simulate_equity(bars, config)
        self.assertEqual(result, simulate_equity(bars, config))
        self.assertEqual([(fill["date"], fill["side"], fill["shares"])
                          for fill in result["fills"]],
                         [(bars[3].date.isoformat(), "BUY", 3),
                          (bars[6].date.isoformat(), "SELL", 3)])
        self.assertEqual(result["equity_curve"][3]["cash"], "59.97")
        self.assertEqual(result["equity_curve"][3]["equity"], "98.97")
        self.assertEqual(result["closed_trades"][0]["net_pnl"], "-11.06")
        self.assertEqual(result["ending_cash"], "88.94")
        self.assertEqual(result["ending_equity"], "88.94")
        self.assertEqual(result["open_shares"], 0)

    def test_open_position_is_marked_to_market_without_futures_margin(self):
        bars = gld_bars([10, 11, 12, 13, 14])
        result = simulate_equity(bars, EquityConfig(starting_cash=Decimal("100"),
                                                     fast_window=2, slow_window=3))
        self.assertEqual(result["open_shares"], 3)
        self.assertEqual(result["ending_cash"], "60.97")
        self.assertEqual(result["ending_equity"], "102.97")
        self.assertEqual(result["fills"][0]["price"], "13.01")
        self.assertIsNone(result["drawdown_halt"])

    def test_rejects_unaffordable_and_over_limit_purchase(self):
        bars = gld_bars([10, 11, 12, 13, 14])
        for config, expected in (
            (EquityConfig(starting_cash=Decimal("5"), fast_window=2, slow_window=3),
             "INSUFFICIENT_CASH"),
            (EquityConfig(starting_cash=Decimal("100"),
                          max_position_fraction=Decimal("0.10"),
                          fast_window=2, slow_window=3), "POSITION_LIMIT"),
        ):
            with self.subTest(reason=expected):
                result = simulate_equity(bars, config)
                self.assertEqual(result["fills"], [])
                self.assertEqual(result["risk_decisions"][0]["reason"], expected)
                self.assertEqual(result["ending_cash"], str(config.starting_cash))

    def test_same_day_close_does_not_change_open_fill(self):
        bars = gld_bars([10, 11, 12, 13, 14])
        config = EquityConfig(starting_cash=Decimal("100"), fast_window=2,
                              slow_window=3)
        original = simulate_equity(bars, config)
        changed = bars.copy()
        changed[3] = replace(changed[3], close=Decimal("12"))
        self.assertEqual(original["fills"][0], simulate_equity(changed, config)["fills"][0])

    def test_drawdown_halts_new_entries_and_sells_next_open(self):
        bars = gld_bars([10, 11, 12, 13, 14, 12, 10, 9])
        result = simulate_equity(bars, EquityConfig(starting_cash=Decimal("100"),
                                                     max_drawdown_fraction=Decimal("0.05"),
                                                     fast_window=2, slow_window=3))
        self.assertTrue(result["halted"])
        self.assertEqual([fill["side"] for fill in result["fills"]], ["BUY", "SELL"])
        self.assertTrue(any(decision["reason"] == "DRAWDOWN_LIMIT"
                            for decision in result["risk_decisions"]))

    def test_drawdown_exit_can_fill_below_threshold_after_overnight_gap(self):
        bars = gld_bars([10, 11, 12, 13, 14, 10, 7, 6])
        config = EquityConfig(starting_cash=Decimal("100"), fast_window=2, slow_window=3)
        result = simulate_equity(bars, config)
        halt = result["drawdown_halt"]
        self.assertEqual(halt["trigger_date"], bars[5].date.isoformat())
        self.assertEqual(halt["exit_date"], bars[6].date.isoformat())
        self.assertEqual(halt["exit_price"], "6.99")
        self.assertFalse(halt["exit_pending"])
        self.assertGreater(Decimal(result["max_drawdown_pct"]), Decimal("10"))
        self.assertEqual(result["risk_decisions"][-1]["reason"], "DRAWDOWN_EXIT")

    def test_drawdown_exit_retries_after_zero_volume(self):
        bars = gld_bars([10, 11, 12, 13, 14, 10, 7, 6])
        bars[6] = replace(bars[6], volume=0)
        result = simulate_equity(bars, EquityConfig(starting_cash=Decimal("100"),
                                                     fast_window=2, slow_window=3))
        self.assertEqual(result["unfilled_orders"][-1]["reason"], "ZERO_VOLUME")
        self.assertEqual(result["drawdown_halt"]["exit_date"], bars[7].date.isoformat())
        self.assertFalse(result["drawdown_halt"]["exit_pending"])

    def test_final_bar_drawdown_leaves_exit_pending(self):
        bars = gld_bars([10, 11, 12, 13, 14, 10])
        result = simulate_equity(bars, EquityConfig(starting_cash=Decimal("100"),
                                                     fast_window=2, slow_window=3))
        self.assertEqual(result["drawdown_halt"]["trigger_date"],
                         bars[-1].date.isoformat())
        self.assertIsNone(result["drawdown_halt"]["exit_date"])
        self.assertTrue(result["drawdown_halt"]["exit_pending"])
        self.assertGreater(result["open_shares"], 0)

    def test_zero_volume_blocks_fill_and_retries_next_day(self):
        bars = gld_bars([10, 11, 12, 13, 14])
        bars[3] = replace(bars[3], volume=0)
        result = simulate_equity(bars, EquityConfig(fast_window=2, slow_window=3))
        self.assertEqual(result["unfilled_orders"][0]["reason"], "ZERO_VOLUME")
        self.assertEqual(result["fills"][0]["date"], bars[4].date.isoformat())

    def test_rejects_non_gld_and_invalid_prices(self):
        bars = gld_bars([10, 11, 12, 13])
        config = EquityConfig(fast_window=2, slow_window=3)
        with self.assertRaisesRegex(ValueError, "GLD only"):
            simulate_equity([replace(bars[0], symbol="MGC"), *bars[1:]], config)
        with self.assertRaisesRegex(ValueError, "finite and positive"):
            simulate_equity([replace(bars[0], close=Decimal("NaN")), *bars[1:]], config)


if __name__ == "__main__":
    unittest.main()
