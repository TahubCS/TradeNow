import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tradenow.__main__ import main
from tradenow.alpaca_paper import (
    Account,
    AlpacaError,
    BrokerOrder,
    Clock,
    DailyBar,
    MixOrder,
    Position,
)
from tradenow.logs import close_logging
from tradenow.mix_config import mix_from_targets
from tradenow.mix_trading import (
    MixStore,
    mix_auto,
    mix_halt,
    mix_report,
    mix_resume,
    mix_status,
)
from tradenow.paper_trading import PaperBlocked, PaperStore, refuse_in_mix_mode
from tradenow.risk_config import LoadedRisk, RiskConfig


D = Decimal
UTC = timezone.utc
MIX = mix_from_targets({"SPY": D("0.6"), "AGG": D("0.4")})


def weekdays(start: date, end: date) -> list[date]:
    days, day = [], start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def risk(auto_submit: bool) -> LoadedRisk:
    return LoadedRisk(RiskConfig(auto_submit=auto_submit), "risk-sha", "test")


class FakeBroker:
    """A paper account: cash, positions, and orders that fill when told to."""

    def __init__(self, cash: str = "10000", positions: dict | None = None):
        self.cash = D(cash)
        self.holdings: dict[str, int] = dict(positions or {})
        self.prices = {"SPY": D(500), "AGG": D(100)}
        self.orders: dict[str, BrokerOrder] = {}
        self.last_equity: Decimal | None = None
        self.is_open = False
        self.set_session(date(2026, 9, 28))
        self.fail_next_submit = False

    def set_session(self, day: date) -> None:
        """The market has closed on `day`; bars exist through it."""
        self.session = day
        self.bar_days = weekdays(day - timedelta(days=40), day)

    def account(self) -> Account:
        equity = self.cash + sum((count * self.prices.get(s, D(10))
                                  for s, count in self.holdings.items()), D(0))
        return Account("ACTIVE", "USD", self.cash, equity, True, False, self.last_equity)

    def clock(self) -> Clock:
        stamp = datetime.combine(self.session, datetime.min.time(), UTC) + timedelta(hours=22)
        following = self.session + timedelta(days=3 if self.session.weekday() == 4 else 1)
        open_at = datetime.combine(following, datetime.min.time(), UTC) + timedelta(hours=13.5)
        return Clock(stamp, self.is_open, open_at, open_at + timedelta(hours=6.5))

    def positions(self) -> list[Position]:
        return [Position(symbol, count) for symbol, count in self.holdings.items() if count]

    def open_orders(self) -> list[BrokerOrder]:
        return [order for order in self.orders.values() if not order.is_final]

    def order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        return self.orders.get(client_order_id)

    def submit_mix(self, order: MixOrder) -> BrokerOrder:
        broker = BrokerOrder(f"b-{order.client_order_id}", order.client_order_id, order.symbol,
                             order.side, order.qty, 0, None, "new")
        self.orders[order.client_order_id] = broker
        if self.fail_next_submit:
            self.fail_next_submit = False
            raise AlpacaError("Alpaca API returned HTTP 504", 504)
        return broker

    def asset_tradable(self, symbol: str) -> bool:
        return True

    def daily_history(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        return [DailyBar(day, self.prices[symbol], self.prices[symbol], 1000)
                for day in self.bar_days if start <= day <= end]

    def cancel_all_orders(self) -> int:
        cancelled = self.open_orders()
        for order in cancelled:
            self.orders[order.client_order_id] = BrokerOrder(
                order.broker_order_id, order.client_order_id, order.symbol, order.side,
                order.qty, 0, None, "canceled")
        return len(cancelled)

    def fill_open_orders(self) -> None:
        for order in self.open_orders():
            price = self.prices[order.symbol]
            sign = 1 if order.side == "buy" else -1
            self.holdings[order.symbol] = self.holdings.get(order.symbol, 0) + sign * order.qty
            self.cash -= sign * order.qty * price
            self.orders[order.client_order_id] = BrokerOrder(
                order.broker_order_id, order.client_order_id, order.symbol, order.side,
                order.qty, order.qty, price, "filled")


class MixTradingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.account_store = PaperStore(root / "alpaca")
        self.store = MixStore(root / "mix", self.account_store)
        self.broker = FakeBroker()
        self.notes: list[tuple[str, str]] = []

    def closes(self, symbols: list[str]) -> dict:
        return {symbol: {day: self.broker.prices[symbol] for day in self.broker.bar_days}
                for symbol in symbols}

    def run_mix(self, auto_submit: bool = True, check_only: bool = False) -> dict:
        return mix_auto(self.broker, self.store, MIX, risk(auto_submit),
                        lambda symbols, end: {"result": "UNCHANGED"}, self.closes,
                        lambda title, message: self.notes.append((title, message)),
                        check_only=check_only)

    def test_first_run_dry_run_plans_without_sending_and_marks_the_account(self):
        result = self.run_mix(auto_submit=False)
        self.assertEqual((result["result"], result["submission"]),
                         ("REBALANCE_BUY", "NOT_SENT_DRY_RUN"))
        self.assertEqual([(o["symbol"], o["qty"], o["limit_price"]) for o in result["orders"]],
                         [("SPY", 11, "505.00"), ("AGG", 39, "101.00")])
        self.assertTrue(self.store.mode_active())
        self.assertEqual(self.store.load_ledger(), ())
        self.assertEqual(self.notes[-1][0], "Mix dry run")
        with self.assertRaises(PaperBlocked) as blocked:
            refuse_in_mix_mode(self.account_store)
        self.assertEqual(blocked.exception.code, "MIX_MODE_ACTIVE")

    def test_invest_fill_then_settle_within_the_band(self):
        first = self.run_mix()
        self.assertEqual([item["result"] for item in first["submission"]],
                         ["SUBMITTED", "SUBMITTED"])
        self.assertEqual(self.run_mix()["result"], "WAIT")  # orders still open
        self.broker.fill_open_orders()
        morning = self.run_mix(check_only=True)
        self.assertEqual(morning["holdings"], {"SPY": 11, "AGG": 39})
        self.assertEqual(len(morning["finished_orders"]), 2)
        # 5,500 SPY and 3,900 AGG of 10,000: both within 5 points of target.
        settled = self.run_mix()
        self.assertEqual((settled["result"], settled["orders"]), ("REBALANCE_NONE", []))
        self.assertEqual(self.run_mix()["result"], "NO_REBALANCE_DUE")
        report = mix_report(self.store)
        self.assertEqual((report["fills"], report["mean_fill_cost_bps"]), (2, "0.00"))

    def test_new_quarter_sells_first_then_buys_on_a_later_evening(self):
        self.run_mix()
        self.broker.fill_open_orders()
        self.run_mix(check_only=True)
        self.run_mix()  # settles within the band in 2026Q3
        self.broker.prices["SPY"] = D(800)  # SPY 8,800 of 13,300: 6.2 points above
        self.broker.set_session(date(2026, 10, 1))
        sell = self.run_mix()
        self.assertEqual([(o["symbol"], o["side"], o["qty"]) for o in sell["orders"]],
                         [("SPY", "sell", 2)])  # keep floor(0.6 x 13,300 / 800) = 9
        self.broker.fill_open_orders()
        self.run_mix(check_only=True)
        self.broker.set_session(date(2026, 10, 2))
        buy = self.run_mix()
        self.assertEqual(buy["result"], "REBALANCE_BUY")
        self.assertEqual([(o["symbol"], o["side"]) for o in buy["orders"]], [("AGG", "buy")])

    def test_first_run_needs_an_empty_account(self):
        self.broker.holdings = {"GLD": 5}
        with self.assertRaises(PaperBlocked) as blocked:
            self.run_mix()
        self.assertEqual(blocked.exception.code, "ACCOUNT_NOT_EMPTY")
        self.assertFalse(self.store.mode_active())

    def test_unexpected_position_engages_the_kill_switch_until_resolved(self):
        self.run_mix(auto_submit=False)  # activates with an empty account
        self.broker.holdings["XYZ"] = 3
        with self.assertRaises(PaperBlocked) as blocked:
            self.run_mix()
        self.assertEqual(blocked.exception.code, "RECONCILIATION_FAILED")
        with self.assertRaises(PaperBlocked) as blocked:
            self.run_mix()
        self.assertEqual(blocked.exception.code, "KILL_SWITCH_ENGAGED")
        with self.assertRaises(PaperBlocked):
            mix_resume(self.broker, self.store)
        del self.broker.holdings["XYZ"]
        self.assertEqual(mix_resume(self.broker, self.store)["result"], "RESUMED")
        self.assertIsNone(self.store.kill_switch())

    def test_negative_cash_and_open_market_are_refused(self):
        self.broker.is_open = True
        with self.assertRaises(PaperBlocked) as blocked:
            self.run_mix()
        self.assertEqual(blocked.exception.code, "MARKET_OPEN")
        self.broker.is_open = False
        self.broker.cash = D(-1)
        with self.assertRaises(PaperBlocked) as blocked:
            self.run_mix()
        self.assertEqual(blocked.exception.code, "MARGIN_IN_USE")
        self.assertIsNotNone(self.store.kill_switch())

    def test_waits_for_tiingo_and_stops_on_mismatched_data(self):
        stale = self.broker.bar_days[:-1]
        waiting = mix_auto(self.broker, self.store, MIX, risk(True),
                           lambda symbols, end: {}, lambda symbols: {
                               symbol: {day: self.broker.prices[symbol] for day in stale}
                               for symbol in symbols}, lambda *_: None)
        self.assertEqual(waiting["result"], "WAITING_FOR_DATA")
        wrong = {symbol: {day: D(1) for day in self.broker.bar_days} for symbol in MIX.targets}
        with self.assertRaises(PaperBlocked) as blocked:
            mix_auto(self.broker, self.store, MIX, risk(True), lambda symbols, end: {},
                     lambda symbols: wrong, lambda *_: None)
        self.assertEqual(blocked.exception.code, "DATA_CHECK_FAILED")

    def test_a_timed_out_submission_is_found_by_client_id(self):
        self.broker.fail_next_submit = True
        result = self.run_mix()
        self.assertEqual([item["result"] for item in result["submission"]],
                         ["SUBMITTED_AFTER_ERROR", "SUBMITTED"])
        self.assertTrue(all(order.status == "new" for order in self.store.load_ledger()))

    def test_drawdown_alerts_are_information_only(self):
        self.run_mix()
        self.broker.fill_open_orders()
        self.run_mix(check_only=True)
        self.run_mix()
        # 11 x 400 + 39 x 90 + 600 cash = 8,510: 14.9% below the 10,000 peak.
        self.broker.prices = {"SPY": D(400), "AGG": D(90)}
        self.broker.set_session(date(2026, 9, 29))
        result = self.run_mix()
        self.assertEqual(result["result"], "NO_REBALANCE_DUE")
        titles = [title for title, _ in self.notes]
        self.assertEqual(titles.count("Mix portfolio drawdown"), 1)
        self.assertEqual(self.broker.open_orders(), [])  # nothing sold

    def test_halt_cancels_and_status_reads_only(self):
        self.run_mix()
        halted = mix_halt(self.broker, self.store, "owner review")
        self.assertEqual(halted["cancelled_orders"], 2)
        status = mix_status(self.broker, self.store, MIX)
        self.assertTrue(status["mode_active"])
        self.assertIn("MIX HALT", status["kill_switch"]["reason"])


class CommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(close_logging)
        self.root = Path(directory.name)
        self.broker = FakeBroker()

    def run_main(self, *argv: str) -> tuple[int, dict | None, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        closes = {symbol: {day: self.broker.prices[symbol] for day in self.broker.bar_days}
                  for symbol in ("SPY", "AGG")}
        with (patch.dict("os.environ", {"TRADENOW_DATA_DIR": str(self.root)}),
              patch("tradenow.__main__.PaperClient", lambda credentials: self.broker),
              patch("tradenow.__main__.load_paper_credentials", lambda: None),
              patch("tradenow.__main__._mix_importer",
                    lambda directory: lambda symbols, end: {"result": "UNCHANGED"}),
              patch("tradenow.__main__._mix_closes",
                    lambda directory: lambda symbols: {s: closes[s] for s in symbols}),
              patch("tradenow.__main__.desktop_notify"),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            code = main(list(argv))
        text = stdout.getvalue()
        return code, json.loads(text) if text.strip() else None, stderr.getvalue()

    def test_plan_needs_a_mix_file(self):
        code, _, stderr = self.run_main("mix-plan")
        self.assertEqual(code, 1)
        self.assertIn("No mix file", stderr)

    def test_plan_is_a_dry_run_and_report_reads_local_files(self):
        (self.root / "mix.toml").write_text("[targets]\nSPY = 0.6\nAGG = 0.4\n",
                                            encoding="utf-8")
        code, printed, _ = self.run_main("mix-plan")
        self.assertEqual(code, 0)
        self.assertEqual((printed["result"], printed["submission"], printed["mode"]),
                         ("REBALANCE_BUY", "NOT_SENT_DRY_RUN", "dry_run"))
        self.assertTrue((self.root / "alpaca" / "mix_mode.json").exists())
        self.assertEqual(self.broker.orders, {})
        code, printed, _ = self.run_main("mix-report")
        self.assertEqual((code, printed["sessions"], printed["orders"]), (0, 1, 0))


if __name__ == "__main__":
    unittest.main()
