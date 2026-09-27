import json
import random
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from tradenow.equity_types import EquityBar
from tradenow.portfolio import (
    PortfolioConfig,
    benchmark_equal_weight,
    benchmark_spy,
    signal_days,
    simulate_portfolio,
)
from tradenow.strategies import History
from tradenow.universe import AssetHistory, Universe


Price = int | str | tuple[int | str, int | str]  # one price, or (open, close)


def weekdays(start: date, count: int) -> list[date]:
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


# Thursday 29 January 2026: bar 1 (Friday 30th) is January's month-end.
MONTH_END_START = date(2026, 1, 29)


def make_universe(prices: Mapping[str, Sequence[Price]], days: list[date] | None = None,
                  zero_volume: Mapping[str, set[int]] | None = None) -> Universe:
    count = len(next(iter(prices.values())))
    days = days or weekdays(MONTH_END_START, count)
    assets = {}
    for symbol, series in prices.items():
        bars = []
        for index, (day, price) in enumerate(zip(days, series, strict=True)):
            open_, close = (price if isinstance(price, tuple) else (price, price))
            o, c = Decimal(str(open_)), Decimal(str(close))
            volume = 0 if index in (zero_volume or {}).get(symbol, set()) else 1000
            bars.append(EquityBar(day, symbol, o, max(o, c), min(o, c), c, volume))
        assets[symbol] = AssetHistory(symbol, bars, {}, 0, 0, "", "")
    return Universe(tuple(prices), days, assets, "synthetic")


def by_signal(*weights: Mapping[str, str]):
    """An allocator returning the n-th weights on the n-th signal day."""
    calls = iter(weights)

    def allocate(_views: Mapping[str, History]) -> dict[str, Decimal]:
        return {symbol: Decimal(value) for symbol, value in next(calls).items()}
    return allocate


CASH = PortfolioConfig(starting_cash=Decimal("1000"))
NO_SLIP = replace(CASH, slippage_per_share=Decimal(0))


def fills(result: dict) -> list[tuple]:
    return [(fill["date"], fill["side"], fill["symbol"], fill["price"], fill["shares"])
            for fill in result["fills"]]


class SignalDayTests(unittest.TestCase):
    def test_first_bar_and_month_ends_but_never_the_last_bar(self):
        days = weekdays(date(2026, 1, 28), 25)  # Wed 28 Jan to Tue 3 Mar
        ends = [days.index(date(2026, 1, 30)), days.index(date(2026, 2, 27))]
        self.assertEqual(signal_days(days), [0, *ends])
        self.assertEqual(signal_days(days, 3), [3, ends[1]])
        # A window ending on a month-end cannot fill after it.
        self.assertEqual(signal_days(days, 0, ends[1] + 1), [0, ends[0]])

    def test_month_end_follows_the_aligned_calendar(self):
        # 31 Dec missing from the calendar makes 30 Dec December's last day.
        days = [date(2025, 12, 29), date(2025, 12, 30), date(2026, 1, 2), date(2026, 1, 5)]
        self.assertEqual(signal_days(days), [0, 1])


class HandCheckedTests(unittest.TestCase):
    def test_two_assets_first_bar_signal_fills_at_second_open(self):
        universe = make_universe({"GLD": [10, (10, 11), 12], "SPY": [20, (20, 19), 18]},
                                 weekdays(date(2026, 1, 5), 3))
        result = simulate_portfolio(universe, by_signal({"GLD": "0.5", "SPY": "0.5"}), CASH)
        # floor(500 / 10.01) = 49 and floor(500 / 20.01) = 24.
        self.assertEqual(fills(result), [("2026-01-06", "BUY", "GLD", "10.01", 49),
                                         ("2026-01-06", "BUY", "SPY", "20.01", 24)])
        self.assertEqual([(day["cash"], day["equity"]) for day in result["equity_curve"]],
                         [("1000", "1000"), ("29.27", "1024.27"), ("29.27", "1049.27")])
        self.assertEqual(result["equity_curve"][2]["positions"], {"GLD": 49, "SPY": 24})
        self.assertEqual(result["total_return_pct"], "4.927")
        self.assertEqual(result["contribution_pct"], {"GLD": "9.751", "SPY": "-4.824"})
        self.assertEqual(result["turnover_pct"], "97.073")
        self.assertEqual(result["closed_trades"], [])

    def test_sells_fill_before_buys_and_fund_them(self):
        universe = make_universe({"GLD": [10, 10, 10, 10], "SPY": [20, 20, 20, 20]})
        result = simulate_portfolio(universe, by_signal({"GLD": "1"}, {"SPY": "1"}), CASH)
        # Only 9.01 is left after the GLD buy, so the SPY buy needs the sale.
        self.assertEqual(fills(result), [("2026-01-30", "BUY", "GLD", "10.01", 99),
                                         ("2026-02-02", "SELL", "GLD", "9.99", 99),
                                         ("2026-02-02", "BUY", "SPY", "20.01", 49)])
        self.assertEqual(result["equity_curve"][2]["cash"], "17.53")
        self.assertEqual(result["closed_trades"], [{
            "symbol": "GLD", "entry_date": "2026-01-30", "exit_date": "2026-02-02",
            "gross_pnl": "-1.98", "net_pnl": "-1.98"}])

    def test_buys_shrink_pro_rata_when_cash_runs_short(self):
        # GLD stays inside its band above target, so the buys want 503 of 500 cash.
        universe = make_universe({"GLD": [10, (10, "10.18"), "10.18", "10.18"],
                                  "SPY": [1, 1, 1, 1], "IEF": [1, 1, 1, 1]})
        result = simulate_portfolio(universe, by_signal(
            {"GLD": "0.5"}, {"GLD": "0.5", "SPY": "0.3", "IEF": "0.2"}), NO_SLIP)
        day = "2026-02-02"
        self.assertIn({"date": day, "symbol": "GLD", "action": "NO_TRADE",
                       "reason": "WITHIN_BAND"}, result["decisions"])
        # floor(302 × 500 / 503) = 300 and floor(201 × 500 / 503) = 199.
        self.assertEqual(fills(result)[1:], [(day, "BUY", "SPY", "1", 300),
                                             (day, "BUY", "IEF", "1", 199)])
        self.assertEqual(result["equity_curve"][2]["cash"], "1")
        self.assertEqual({d["reason"] for d in result["decisions"] if d["action"] == "BUY"
                          and d["date"] == day}, {"SHRUNK_TO_CASH"})

    def test_one_percent_band_is_exclusive(self):
        universe = make_universe({"GLD": [10, 10, 10, 10]})
        for weight, expected in (("0.99", []), ("0.989", [("2026-02-02", "SELL", "GLD",
                                                            "10", 2)])):
            with self.subTest(weight=weight):
                # Holding 1000 against a target of 990 differs by exactly 1%.
                result = simulate_portfolio(universe, by_signal({"GLD": "1"},
                                                                {"GLD": weight}), NO_SLIP)
                self.assertEqual(fills(result)[1:], expected)

    def test_target_zero_sells_a_position_smaller_than_the_band(self):
        universe = make_universe({"GLD": [1, 1, "0.4", "0.4"], "SPY": [1, 1, 1, 1]})
        for weight, expected in (("0", [("2026-02-02", "SELL", "GLD", "0.4", 20)]),
                                 ("0.001", [])):
            with self.subTest(weight=weight):
                # 20 shares worth 8 are under 1% of the 988 equity.
                result = simulate_portfolio(universe, by_signal(
                    {"GLD": "0.02"}, {"GLD": weight}), NO_SLIP)
                self.assertEqual(fills(result)[1:], expected)
        self.assertEqual(len(result["closed_trades"]), 0)


class HaltTests(unittest.TestCase):
    # Mon 26 Jan: bar 4 (Fri 30 Jan) is a month-end signal after the halt.
    DAYS = weekdays(date(2026, 1, 26), 6)
    GLD = [10, 10, (10, "8.9"), ("8.8", 9), 10, 12]

    def test_halt_sells_next_open_and_blocks_new_entries(self):
        universe = make_universe({"GLD": self.GLD}, self.DAYS)
        result = simulate_portfolio(universe, by_signal({"GLD": "1"}, {"GLD": "1"}), CASH)
        self.assertEqual(fills(result), [("2026-01-27", "BUY", "GLD", "10.01", 99),
                                         ("2026-01-29", "SELL", "GLD", "8.79", 99)])
        halt = result["drawdown_halt"]
        self.assertEqual((halt["trigger_date"], halt["trigger_drawdown_pct"],
                          halt["exit_date"], halt["exit_pending"]),
                         ("2026-01-28", "10.989", "2026-01-29", False))
        self.assertEqual(result["signals"][-1]["reason"], "DRAWDOWN_HALT")
        self.assertEqual(result["closed_trades"][0]["net_pnl"], "-120.78")
        self.assertEqual(result["ending_equity"], "879.22")

    def test_halt_sale_retries_after_zero_volume(self):
        universe = make_universe({"GLD": self.GLD}, self.DAYS, {"GLD": {3}})
        result = simulate_portfolio(universe, by_signal({"GLD": "1"}, {"GLD": "1"}), CASH)
        self.assertEqual(result["unfilled_orders"],
                         [{"date": "2026-01-29", "symbol": "GLD", "reason": "ZERO_VOLUME"}])
        self.assertEqual(fills(result)[1:], [("2026-01-30", "SELL", "GLD", "9.99", 99)])
        self.assertEqual(result["drawdown_halt"]["exit_date"], "2026-01-30")

    def test_halt_on_the_last_bar_leaves_the_exit_pending(self):
        universe = make_universe({"GLD": self.GLD[:3]}, self.DAYS[:3])
        result = simulate_portfolio(universe, by_signal({"GLD": "1"}), CASH)
        self.assertTrue(result["drawdown_halt"]["exit_pending"])
        self.assertEqual(result["open_positions"], {"GLD": 99})

    def test_halt_can_be_disabled(self):
        universe = make_universe({"GLD": self.GLD}, self.DAYS)
        result = simulate_portfolio(universe, by_signal({"GLD": "1"}, {"GLD": "1"}),
                                    replace(CASH, drawdown_halt=False))
        self.assertFalse(result["halted"])
        self.assertEqual(len(result["fills"]), 1)


class BlockedOrderTests(unittest.TestCase):
    def test_zero_volume_rebalance_retries_and_is_resized(self):
        universe = make_universe({"GLD": [10, 10, 20, 20]}, weekdays(date(2026, 1, 5), 4),
                                 {"GLD": {1}})
        result = simulate_portfolio(universe, by_signal({"GLD": "1"}), CASH)
        # Retried at the next open and sized from that day's price.
        self.assertEqual(fills(result), [("2026-01-07", "BUY", "GLD", "20.01", 49)])

    def test_newer_signal_replaces_a_blocked_order(self):
        universe = make_universe({"GLD": [10, 10, 10, 10], "SPY": [20, 20, 20, 20]},
                                 zero_volume={"GLD": {1}})
        result = simulate_portfolio(universe, by_signal({"GLD": "1"}, {"SPY": "1"}), CASH)
        self.assertEqual(fills(result), [("2026-02-02", "BUY", "SPY", "20.01", 49)])

    def test_stale_gap_blocks_the_order(self):
        days = [date(2026, 1, 5), date(2026, 1, 16), date(2026, 1, 19)]
        universe = make_universe({"GLD": [10, 10, 10]}, days)
        result = simulate_portfolio(universe, by_signal({"GLD": "1"}), CASH)
        self.assertEqual(result["unfilled_orders"][0]["reason"], "STALE_BAR_GAP")
        self.assertEqual(fills(result), [("2026-01-19", "BUY", "GLD", "10.01", 99)])


class BenchmarkTests(unittest.TestCase):
    def test_b1_equal_weights_rebalanced_monthly_without_halt(self):
        universe = make_universe({"GLD": [10, (10, 12), 12, (12, 6)],
                                  "SPY": [20, 20, 20, 20]})
        result = benchmark_equal_weight(universe, CASH)
        self.assertEqual(fills(result), [
            ("2026-01-30", "BUY", "GLD", "10.01", 49), ("2026-01-30", "BUY", "SPY", "20.01", 24),
            # Equity 1097.27 at the open: GLD down to 45 shares, SPY up to 27.
            ("2026-02-02", "SELL", "GLD", "11.99", 4), ("2026-02-02", "BUY", "SPY", "20.01", 3)])
        self.assertEqual(result["equity_curve"][2]["equity"], "1097.20")
        self.assertFalse(result["halted"])  # a 25% fall does not halt a benchmark
        self.assertEqual(result["closed_trades"], [])  # trims are not round trips

    def test_b2_buys_spy_once_and_holds(self):
        universe = make_universe({"GLD": [10, 10, 10, 10],
                                  "SPY": [20, 20, (30, 30), (30, 15)]})
        result = benchmark_spy(universe, CASH)
        self.assertEqual(fills(result), [("2026-01-30", "BUY", "SPY", "20.01", 49)])
        self.assertEqual([s["date"] for s in result["signals"]], ["2026-01-29"])
        self.assertEqual(result["ending_equity"], "754.51")
        self.assertFalse(result["halted"])

    def test_b2_needs_spy(self):
        with self.assertRaises(ValueError):
            benchmark_spy(make_universe({"GLD": [10, 10]}), CASH)


class BoundaryTests(unittest.TestCase):
    def test_other_data_is_validated_after_a_valid_run(self):
        universe = make_universe({"GLD": [10, 10, 10]})
        simulate_portfolio(universe, lambda _views: {"GLD": Decimal(1)}, CASH)
        bad = make_universe({"GLD": [10, 10, 10]})
        bars = bad.assets["GLD"].bars
        bars[1] = replace(bars[1], close=Decimal(0))
        with self.assertRaisesRegex(ValueError, "Invalid GLD bar"):
            simulate_portfolio(bad, lambda _views: {"GLD": Decimal(1)}, CASH)

    def test_rejects_invalid_weights(self):
        universe = make_universe({"GLD": [10, 10, 10], "SPY": [20, 20, 20]})
        for weights in ({"GLD": Decimal("0.6"), "SPY": Decimal("0.4000001")},
                        {"GLD": Decimal("-0.1")}, {"DBC": Decimal("0.1")},
                        {"GLD": Decimal("NaN")}, {"GLD": 0.5}):
            with self.subTest(weights=weights), self.assertRaises(ValueError):
                simulate_portfolio(universe, lambda _views, w=weights: w, CASH)

    def test_allocator_sees_only_history_up_to_the_signal_close(self):
        universe = make_universe({"GLD": [10] * 30}, weekdays(date(2026, 1, 5), 30))
        seen = []

        def allocate(views: Mapping[str, History]) -> dict[str, Decimal]:
            view = views["GLD"]
            seen.append((len(view), view.today.date))
            with self.assertRaises(IndexError):
                view[len(view)]
            return {}
        simulate_portfolio(universe, allocate, CASH, start=4)
        self.assertEqual(seen, [(5, date(2026, 1, 9)), (20, date(2026, 1, 30))])

    def test_window_starts_from_cash_at_its_own_first_bar(self):
        universe = make_universe({"GLD": [10, 10, 5, 10, 10, 10]},
                                 weekdays(date(2026, 1, 5), 6))
        whole = simulate_portfolio(universe, lambda _v: {"GLD": Decimal(1)}, CASH)
        window = simulate_portfolio(universe, lambda _v: {"GLD": Decimal(1)}, CASH, start=3)
        self.assertTrue(whole["halted"])
        self.assertFalse(window["halted"])
        self.assertEqual(window["equity_curve"][0], {"date": "2026-01-08", "cash": "1000",
                                                     "equity": "1000",
                                                     "positions": {"GLD": 0}})
        self.assertEqual(fills(window), [("2026-01-09", "BUY", "GLD", "10.01", 99)])


class InvariantTests(unittest.TestCase):
    def random_run(self, seed: int) -> tuple[Universe, dict]:
        rng = random.Random(seed)
        symbols = ("GLD", "SLV", "SPY", "EFA", "IEF", "DBC")
        count = 160
        prices = {}
        for symbol in symbols:
            price, series = Decimal(rng.randint(20, 200)), []
            for _ in range(count):
                open_ = max(Decimal(1), price * Decimal(rng.randint(96, 104)) / 100)
                price = max(Decimal(1), open_ * Decimal(rng.randint(95, 105)) / 100)
                series.append((str(open_.quantize(Decimal("0.01"))),
                               str(price.quantize(Decimal("0.01")))))
            prices[symbol] = series
        zero = {symbol: {rng.randrange(count) for _ in range(3)} for symbol in symbols}
        universe = make_universe(prices, weekdays(date(2024, 1, 3), count), zero)

        def allocate(_views: Mapping[str, History]) -> dict[str, Decimal]:
            raw = {symbol: Decimal(rng.randint(0, 4)) for symbol in symbols}
            total = sum(raw.values()) or Decimal(1)
            return {s: (v / total).quantize(Decimal("0.0001"), "ROUND_DOWN")
                    for s, v in raw.items()}
        config = replace(PortfolioConfig(starting_cash=Decimal("20000")),
                         max_drawdown_fraction=Decimal("0.5"))
        return universe, simulate_portfolio(universe, allocate, config)

    def test_cash_shares_order_and_contribution_invariants(self):
        for seed in range(5):
            with self.subTest(seed=seed):
                universe, result = self.random_run(seed)
                self.assertGreater(len(result["fills"]), 20)
                for day in result["equity_curve"]:
                    self.assertGreaterEqual(Decimal(day["cash"]), 0)
                    for shares in day["positions"].values():
                        self.assertIsInstance(shares, int)
                        self.assertGreaterEqual(shares, 0)
                for signal in result["signals"]:
                    self.assertLessEqual(sum(map(Decimal, signal["weights"].values())), 1)
                sides: dict[str, list[str]] = {}
                for fill in result["fills"]:
                    sides.setdefault(fill["date"], []).append(fill["side"])
                for day_sides in sides.values():
                    self.assertEqual(day_sides, sorted(day_sides, reverse=True))  # SELL first
                contribution = sum(map(Decimal, result["contribution_pct"].values()))
                self.assertLess(abs(contribution - Decimal(result["total_return_pct"])),
                                Decimal("0.01"))

    def test_identical_inputs_give_byte_identical_results(self):
        first = json.dumps(self.random_run(7)[1], sort_keys=True)
        self.assertEqual(first, json.dumps(self.random_run(7)[1], sort_keys=True))
        universe = make_universe({"GLD": [10, (10, 12), 12, (12, 6)], "SPY": [20, 20, 20, 20]})
        self.assertEqual(json.dumps(benchmark_equal_weight(universe, CASH), sort_keys=True),
                         json.dumps(benchmark_equal_weight(universe, CASH), sort_keys=True))


if __name__ == "__main__":
    unittest.main()
