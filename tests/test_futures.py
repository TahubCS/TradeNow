import csv
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO

from tradenow.market_data import Bar, parse_bars
from tradenow.offline import run_local_csv
from tradenow.simulation import Config, simulate
from tradenow.stress import audit_simulation
from tradenow.synthetic import generate_bars


def rising_bars(count=12):
    bars = []
    day = date(2026, 1, 5)
    while len(bars) < count:
        if day.weekday() < 5:
            price = Decimal(2000 + len(bars))
            bars.append(Bar(day, "MGCZ26", price, price + 1, price - 1,
                            price, 100))
        day += timedelta(days=1)
    return bars


class FuturesTests(unittest.TestCase):
    def test_roll_closes_old_contract_and_resets_signal_warmup(self):
        bars = rising_bars()
        bars = [replace(bar, contract="MGCG27", open=bar.open + 200,
                        high=bar.high + 200, low=bar.low + 200,
                        close=bar.close + 200) if index >= 6 else bar
                for index, bar in enumerate(bars)]
        result = simulate(bars, Config(fast_window=2, slow_window=3))
        audit_simulation(result)
        self.assertEqual(len(result["rolls"]), 1)
        exits = [fill for fill in result["fills"] if fill["execution_point"] == "CLOSE"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["contract"], "MGCZ26")
        self.assertEqual(exits[0]["date"], bars[5].date.isoformat())
        self.assertEqual(result["equity_curve"][6]["position_contracts"], 0)
        self.assertLess(abs(Decimal(result["total_pnl"])), Decimal("1000"))
        self.assertTrue(any(fill["side"] == "BUY" and fill["contract"] == "MGCG27"
                            for fill in result["fills"]))
        blocked = bars.copy()
        blocked[5] = replace(blocked[5], volume=0)
        with self.assertRaisesRegex(ValueError, "untradable roll/expiry"):
            simulate(blocked, Config(fast_window=2, slow_window=3))

    def test_initial_and_maintenance_margin(self):
        bars = rising_bars(8)
        config = Config(starting_cash=Decimal("5000"),
                        max_notional_fraction=Decimal("5"),
                        initial_margin_fraction=Decimal("0.30"),
                        maintenance_margin_fraction=Decimal("0.20"),
                        fast_window=2, slow_window=3)
        rejected = simulate(bars, config)
        self.assertFalse(rejected["fills"])
        self.assertTrue(any(d["reason"] == "INITIAL_MARGIN"
                            for d in rejected["risk_decisions"]))

        stressed = bars.copy()
        stressed[4] = replace(stressed[4], open=Decimal("1500"),
                              high=Decimal("1501"), low=Decimal("1499"),
                              close=Decimal("1500"))
        held = simulate(stressed, replace(config, starting_cash=Decimal("7000"),
                                        max_drawdown_fraction=Decimal("1")))
        self.assertTrue(any(d["reason"] == "MAINTENANCE_MARGIN"
                            for d in held["risk_decisions"]))
        self.assertTrue(held["halted"])
        self.assertEqual(held["fills"][1]["side"], "SELL")

    def test_unfilled_orders_report_session_liquidity_and_stale_gap(self):
        bars = rising_bars(9)
        bars[3] = replace(bars[3], volume=0)
        bars[4] = replace(bars[4], open_time_ct=datetime(2026, 1, 9, 16, 30,
                                                        tzinfo=timezone(timedelta(hours=-6))))
        for index in range(5, len(bars)):
            bars[index] = replace(bars[index], date=bars[index].date + timedelta(days=10))
        result = simulate(bars, Config(fast_window=2, slow_window=3))
        self.assertEqual([item["reason"] for item in result["unfilled_orders"][:3]],
                         ["ZERO_VOLUME", "SESSION_CLOSED", "STALE_BAR_GAP"])
        self.assertTrue(result["fills"])

    def test_expiry_guard_exits_before_last_trade_date(self):
        bars = rising_bars(9)
        last_trade = bars[-1].date + timedelta(days=5)
        bars = [replace(bar, last_trade_date=last_trade) for bar in bars]
        result = simulate(bars, Config(fast_window=2, slow_window=3))
        exits = [d for d in result["risk_decisions"] if d["reason"] == "EXPIRY_EXIT"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(result["open_contracts"], 0)
        self.assertLess(exits[0]["date"], last_trade.isoformat())

    def test_local_roll_csv_records_metadata_and_rejects_bad_dates(self):
        bars = generate_bars(days=180)
        source = StringIO(newline="")
        writer = csv.writer(source, lineterminator="\n")
        writer.writerow(("date", "contract", "open", "high", "low", "close",
                         "volume", "last_trade_date"))
        for index, bar in enumerate(bars):
            shift = Decimal("100") if index >= 160 else Decimal("0")
            writer.writerow((bar.date, "MGCG26" if index >= 160 else "MGCZ25",
                             bar.open + shift, bar.high + shift, bar.low + shift,
                             bar.close + shift, bar.volume,
                             "2026-06-01" if index >= 160 else "2026-01-01"))
        report, _ = run_local_csv(source.getvalue().encode(), "rolls.csv")
        self.assertEqual(report["data"]["contracts"], ["MGCZ25", "MGCG26"])
        self.assertTrue(report["data"]["last_trade_dates_provided"])
        self.assertEqual(len(report["research"]["holdout_result"]["rolls"]), 1)

        bad = "date,contract,open,high,low,close,volume,last_trade_date\n"
        bad += "2026-01-05,MGCZ26,2000,2001,1999,2000,10,2026-01-05\n"
        with self.assertRaisesRegex(ValueError, "last trade date"):
            parse_bars(StringIO(bad))
        bad_time = "date,contract,open,high,low,close,volume,open_time_ct\n"
        bad_time += "2026-01-05,MGCZ26,2000,2001,1999,2000,10,2026-01-04T17:00:00+00:00\n"
        with self.assertRaisesRegex(ValueError, "Chicago UTC offset"):
            parse_bars(StringIO(bad_time))


if __name__ == "__main__":
    unittest.main()
