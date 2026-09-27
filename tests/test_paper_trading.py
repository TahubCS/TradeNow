import json
import math
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from tradenow.alpaca_paper import (
    Account,
    AlpacaError,
    BrokerOrder,
    Clock,
    DailyBar,
    PaperOrder,
    Position,
)
from tradenow.equity import EquityConfig
from tradenow.gld_research import parse_gld_csv
from tradenow.paper_rules import NOT_FOUND, SUBMITTING
from tradenow.paper_trading import (
    PaperBlocked,
    PaperStore,
    _reconciled,
    paper_halt,
    paper_plan,
    paper_report,
    paper_resume,
    paper_status,
    paper_submit,
)


EASTERN = timezone(timedelta(hours=-5))


def gld_csv(bars: int = 241) -> bytes:
    """A rising, wavy series: SMA 3/10 is selected and signals long on the last bar."""
    rows = ["date,symbol,open,high,low,close,volume,adj_open,adj_high,adj_low,"
            "adj_close,adj_volume,div_cash,split_factor"]
    day, index = date(2025, 1, 6), 0
    while index < bars:
        if day.weekday() < 5:
            close = Decimal(str(round(300 + index * 0.5 + 8 * math.sin(index / 6), 2)))
            rows.append(f"{day},GLD,{close},{close + 1},{close - 1},{close},1000,"
                        f"{close},{close + 1},{close - 1},{close},1000,0.0,1.0")
            index += 1
        day += timedelta(days=1)
    return ("\n".join(rows) + "\n").encode()


SOURCE = gld_csv()
BARS = parse_gld_csv(SOURCE, EquityConfig())
LAST = BARS[-1]  # Monday 2025-12-08


class FakeBroker:
    """In-memory Alpaca paper account; fills are applied explicitly by the test."""

    def __init__(self):
        self.cash = Decimal("100000")
        self.equity = Decimal("100000")
        self.paper = True
        self.is_open = False
        self.next_open = datetime(2025, 12, 9, 9, 30, tzinfo=EASTERN)
        self.now = datetime(2025, 12, 8, 20, 0, tzinfo=EASTERN)
        self.shares = 0
        self.orders: dict[str, BrokerOrder] = {}
        self.bars = [DailyBar(bar.date, bar.open, bar.close, bar.volume)
                     for bar in BARS if bar.date >= LAST.date - timedelta(days=21)]
        self.submit_error: AlpacaError | None = None
        self.store_on_error = False
        self.submissions = 0
        self.cancels = 0
        self.offline = False

    def _check(self):
        if self.offline:
            raise AlpacaError("Alpaca API connection failed")

    def account(self):
        self._check()
        return Account("ACTIVE", "USD", self.cash, self.equity, self.paper, False)

    def clock(self):
        self._check()
        return Clock(self.now, self.is_open, self.next_open, self.next_open + timedelta(hours=6))

    def positions(self):
        self._check()
        return [Position("GLD", self.shares)] if self.shares else []

    def open_orders(self):
        self._check()
        return [order for order in self.orders.values() if not order.is_final]

    def gld_tradable(self):
        return True

    def order_by_client_id(self, client_order_id):
        self._check()
        return self.orders.get(client_order_id)

    def submit_gld(self, order: PaperOrder):
        self._check()
        self.submissions += 1
        broker = BrokerOrder(f"b{self.submissions}", order.client_order_id, "GLD",
                             order.side, order.qty, 0, None, "accepted")
        if self.submit_error is not None:
            if self.store_on_error:
                self.orders[order.client_order_id] = broker
            raise self.submit_error
        self.orders[order.client_order_id] = broker
        return broker

    def cancel_all_orders(self):
        self._check()
        open_ids = [key for key, order in self.orders.items() if not order.is_final]
        for key in open_ids:
            self.orders[key] = replace(self.orders[key], status="canceled")
        self.cancels += 1
        return len(open_ids)

    def gld_daily_bars(self, start, end):
        self._check()
        return [bar for bar in self.bars if bar.date >= start]

    def fill(self, client_order_id: str, price: Decimal):
        order = self.orders[client_order_id]
        self.orders[client_order_id] = replace(order, status="filled",
                                               filled_qty=order.qty, filled_avg_price=price)
        sign = 1 if order.side == "buy" else -1
        self.shares += sign * order.qty
        self.cash -= sign * order.qty * price


class PaperWorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PaperStore(Path(directory.name))
        self.broker = FakeBroker()

    def plan(self) -> dict:
        return paper_plan(self.broker, self.store, SOURCE, "GLD.csv")

    def test_plan_then_approved_submit_sends_exactly_once(self):
        plan = self.plan()
        self.assertEqual(plan["selected_hypothesis"], "sma_3_10")
        self.assertEqual(plan["action"], "BUY")
        order = plan["order"]
        limit = Decimal(order["limit_price"])
        self.assertEqual(limit, (LAST.close * Decimal("1.01")).quantize(Decimal("0.01")))
        self.assertEqual(order["qty"], int(Decimal("50000") / limit))
        self.assertEqual(order["client_order_id"],
                         f"tn-gld-20251209-buy-{plan['plan_id']}")
        self.assertEqual(self.broker.submissions, 0)

        first = paper_submit(self.broker, self.store, plan["plan_id"])
        self.assertEqual(first["result"], "SUBMITTED")
        again = paper_submit(self.broker, self.store, plan["plan_id"])
        self.assertEqual(again["result"], "ALREADY_SUBMITTED")
        self.assertEqual(self.broker.submissions, 1)

        self.broker.fill(order["client_order_id"], limit)
        status = paper_status(self.broker, self.store)
        self.assertTrue(status["reconciliation"]["ok"])
        self.assertEqual(status["reconciliation"]["ledger_shares"], order["qty"])

    def test_plan_is_rejected_while_market_is_open_or_after_its_session(self):
        self.broker.is_open = True
        with self.assertRaisesRegex(PaperBlocked, "MARKET_OPEN"):
            self.plan()
        self.broker.is_open = False
        plan = self.plan()
        self.broker.next_open += timedelta(days=1)
        with self.assertRaisesRegex(PaperBlocked, "PLAN_EXPIRED"):
            paper_submit(self.broker, self.store, plan["plan_id"])
        self.assertEqual(self.broker.submissions, 0)

    def test_stale_or_disagreeing_data_blocks_planning(self):
        self.broker.bars.append(DailyBar(date(2025, 12, 9), LAST.close, LAST.close, 1))
        with self.assertRaisesRegex(PaperBlocked, "TIINGO_STALE"):
            self.plan()
        self.broker.bars.pop()
        last = self.broker.bars[-1]
        self.broker.bars[-1] = replace(last, close=last.close * Decimal("1.02"))
        with self.assertRaisesRegex(PaperBlocked, "CLOSE_MISMATCH"):
            self.plan()
        self.assertFalse(self.store.plans_dir.exists())

    def test_manual_position_engages_kill_switch_until_reconciled(self):
        self.broker.shares = 5
        with self.assertRaisesRegex(PaperBlocked, "RECONCILIATION_FAILED"):
            self.plan()
        self.assertIn("POSITION_MISMATCH", self.store.kill_switch()["reason"])
        self.broker.shares = 0
        with self.assertRaisesRegex(PaperBlocked, "KILL_SWITCH_ENGAGED"):
            self.plan()
        self.broker.shares = 5
        with self.assertRaisesRegex(PaperBlocked, "RECONCILIATION_FAILED"):
            paper_resume(self.broker, self.store)
        self.broker.shares = 0
        self.assertEqual(paper_resume(self.broker, self.store)["kill_switch"], "CLEARED")
        self.assertEqual(self.plan()["action"], "BUY")

    def test_kill_switch_blocks_approved_plan_and_engages_without_network(self):
        plan = self.plan()
        self.broker.offline = True
        result = paper_halt(self.broker, self.store, "manual test", flatten=False)
        self.assertIn("connection failed", result["error"])
        self.assertIsNotNone(self.store.kill_switch())
        self.broker.offline = False
        with self.assertRaisesRegex(PaperBlocked, "KILL_SWITCH_ENGAGED"):
            paper_submit(self.broker, self.store, plan["plan_id"])
        self.assertEqual(self.broker.submissions, 0)

    def test_halt_flatten_cancels_orders_and_sells_position(self):
        plan = self.plan()
        paper_submit(self.broker, self.store, plan["plan_id"])
        self.broker.fill(plan["order"]["client_order_id"], LAST.close)
        result = paper_halt(self.broker, self.store, "stop", flatten=True)
        self.assertEqual(result["flatten"]["result"], "SUBMITTED")
        self.assertEqual(result["flatten"]["order"]["side"], "sell")
        self.assertEqual(result["flatten"]["order"]["qty"], plan["order"]["qty"])
        self.assertEqual(self.broker.cancels, 1)

    def test_timeout_after_alpaca_accepts_is_recorded_not_resent(self):
        plan = self.plan()
        self.broker.submit_error = AlpacaError("Alpaca API connection failed")
        self.broker.store_on_error = True
        result = paper_submit(self.broker, self.store, plan["plan_id"])
        self.assertEqual(result["result"], "SUBMITTED_AFTER_ERROR")
        self.broker.submit_error = None
        again = paper_submit(self.broker, self.store, plan["plan_id"])
        self.assertEqual(again["result"], "ALREADY_SUBMITTED")
        self.assertEqual(self.broker.submissions, 1)

    def test_submission_that_never_arrived_is_resolved_on_next_run(self):
        plan = self.plan()
        self.broker.submit_error = AlpacaError("Alpaca API connection failed")
        with self.assertRaisesRegex(PaperBlocked, "SUBMIT_FAILED"):
            paper_submit(self.broker, self.store, plan["plan_id"])
        self.assertEqual(self.store.load_ledger().orders[0].status, SUBMITTING)
        self.broker.submit_error = None
        replanned = self.plan()
        self.assertEqual(self.store.load_ledger().orders[0].status, NOT_FOUND)
        self.assertEqual(replanned["action"], "BUY")

    def test_drawdown_breach_sells_and_blocks_new_entries(self):
        plan = self.plan()
        paper_submit(self.broker, self.store, plan["plan_id"])
        self.broker.fill(plan["order"]["client_order_id"], LAST.close)
        self.broker.now += timedelta(days=1)
        self.broker.next_open += timedelta(days=1)
        self.broker.equity = Decimal("89000")
        exit_plan = self.plan()
        self.assertEqual((exit_plan["action"], exit_plan["reason"]), ("SELL", "DRAWDOWN_EXIT"))
        self.assertTrue(exit_plan["drawdown"]["halted"])
        paper_submit(self.broker, self.store, exit_plan["plan_id"])
        self.broker.fill(exit_plan["order"]["client_order_id"], LAST.close)
        self.broker.equity = Decimal("100000")
        self.broker.next_open += timedelta(days=1)
        later = self.plan()
        self.assertEqual((later["action"], later["target"]), ("NO_TRADE", 0))
        self.assertIsNotNone(self.store.load_ledger().drawdown_halt)

    def test_fill_is_measured_against_plan_open_and_simulation(self):
        plan = self.plan()
        paper_submit(self.broker, self.store, plan["plan_id"])
        client_order_id = plan["order"]["client_order_id"]
        recorded = self.store.load_ledger().orders[0]
        self.assertEqual(recorded.reference_close, LAST.close)
        self.assertEqual(recorded.sent_at, self.broker.now)

        # The next session (2025-12-09) opens at its close in this synthetic series.
        extended = gld_csv(242)
        session_open = parse_gld_csv(extended, EquityConfig())[-1].open
        fill_price = session_open + Decimal("0.10")
        self.broker.fill(client_order_id, fill_price)
        opened = self.broker.next_open
        self.broker.orders[client_order_id] = replace(
            self.broker.orders[client_order_id], submitted_at=self.broker.now,
            filled_at=opened + timedelta(seconds=2))
        _reconciled(self.broker, self.store, engage_on_mismatch=True)

        with tempfile.TemporaryDirectory() as logs:
            report = paper_report(self.store, extended, "GLD.csv", Path(logs))
        row = report["orders"][0]
        self.assertEqual(row["outcome"], "FILLED")
        self.assertEqual(Decimal(row["reference"]["open"]), session_open)
        self.assertEqual(Decimal(row["reference"]["simulated"]),
                         session_open + EquityConfig().slippage_per_share)
        self.assertGreater(Decimal(row["slippage_bps"]["open"]), 0)
        self.assertEqual(Decimal(row["cost_vs_simulation"]),
                         (Decimal("0.09") * plan["order"]["qty"]).quantize(Decimal("0.01")))
        self.assertEqual(Decimal(row["latency_s"]["open_to_fill"]), Decimal("2.000"))
        self.assertEqual(report["summary"]["fill_rate_pct"], "100.00")
        self.assertEqual(report["runs"]["total"], 0)

        # Without the session's bar yet, the open-based comparison waits for the next import.
        waiting = paper_report(self.store, SOURCE, "GLD.csv", Path(self.store.directory))
        self.assertEqual(waiting["summary"]["orders_awaiting_open_price"], 1)
        self.assertEqual(set(waiting["orders"][0]["slippage_bps"]), {"plan"})

    def test_schema_1_ledger_without_execution_fields_still_loads(self):
        self.store.directory.mkdir(parents=True, exist_ok=True)
        self.store.ledger_path.write_text(json.dumps({
            "schema_version": 1, "peak_equity": "100000", "drawdown_halt": None,
            "orders": [{"client_order_id": "tn-gld-20251209-buy-x", "plan_id": "p",
                        "session": "2025-12-09", "side": "buy", "qty": 1,
                        "limit_price": "300.00", "status": "filled", "filled_qty": 1,
                        "filled_avg_price": "299.50", "broker_order_id": "b1"}]}),
            encoding="utf-8")
        ledger = self.store.load_ledger()
        self.assertIsNone(ledger.orders[0].filled_at)
        self.store.save_ledger(ledger)
        saved = json.loads(self.store.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["schema_version"], 2)
        self.assertEqual(self.store.load_ledger(), ledger)

    def test_tampered_plan_is_refused(self):
        plan = self.plan()
        path = self.store.plans_dir / f"{plan['plan_id']}.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["order"]["qty"] = 999
        path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaisesRegex(PaperBlocked, "PLAN_TAMPERED"):
            paper_submit(self.broker, self.store, plan["plan_id"])

    def test_dashboard_view_shows_latest_plan_without_side_effects(self):
        from tradenow.web import paper_view
        self.assertIsNone(paper_view(self.store, self.broker)["latest_plan"])
        plan = self.plan()
        view = paper_view(self.store, self.broker)
        self.assertEqual(view["latest_plan"]["plan_id"], plan["plan_id"])
        self.assertTrue(view["status"]["reconciliation"]["ok"])
        self.assertIsNone(view["status"]["kill_switch"])
        self.assertEqual(self.broker.submissions, 0)

    def test_live_account_is_refused(self):
        self.broker.paper = False
        with self.assertRaisesRegex(PaperBlocked, "NOT_PAPER_ACCOUNT"):
            self.plan()


if __name__ == "__main__":
    unittest.main()
