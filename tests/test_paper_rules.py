import unittest
from datetime import date, timedelta
from decimal import Decimal

from tradenow.alpaca_paper import BrokerOrder, DailyBar, Position
from tradenow.equity import EquityBar, EquityConfig, feature_rows, plain_target, simulate_equity
from tradenow.paper_rules import (
    NOT_FOUND,
    SUBMITTING,
    LedgerOrder,
    apply_broker_order,
    cross_check_closes,
    measure_drawdown,
    plan_order,
    reconcile,
)
from tradenow.strategies import History, SmaCross


def gld_bars(closes: list[str], start: date = date(2026, 1, 5)) -> list[EquityBar]:
    bars, day = [], start
    for value in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        price = Decimal(value)
        bars.append(EquityBar(day, "GLD", price, price + 1, price - 1, price, 100))
        day += timedelta(days=1)
    return bars


def alpaca_from(bars: list[EquityBar]) -> list[DailyBar]:
    return [DailyBar(bar.date, bar.open, bar.close, bar.volume) for bar in bars]


def ledger_order(side: str = "buy", qty: int = 10, filled: int = 10,
                 status: str = "filled", client_id: str = "tn-gld-a") -> LedgerOrder:
    return LedgerOrder(client_id, "plan", date(2026, 9, 28), side, qty,
                       Decimal("400.00") if side == "buy" else None, status, filled)


def broker_order(client_id: str = "tn-gld-a", status: str = "new",
                 filled: int = 0) -> BrokerOrder:
    return BrokerOrder("b1", client_id, "GLD", "buy", 10, filled, None, status)


class SignalTests(unittest.TestCase):
    def test_signal_matches_backtest_rule_on_every_day(self):
        closes = [str(100 + (index * 7) % 11 - index // 3) for index in range(40)]
        bars = gld_bars(closes)
        config = EquityConfig(fast_window=3, slow_window=10)
        simulated = simulate_equity(bars, config)["signals"]
        strategy = SmaCross(3, 10)
        for index, signal in enumerate(simulated, start=config.slow_window - 1):
            # paper-plan decides from history ending at that close, as here.
            rows = feature_rows(bars[:index + 1], strategy)
            live = strategy.decide(History(rows, index), holding=False)
            self.assertEqual(plain_target(live.target), signal["strategy_target"])


class CrossCheckTests(unittest.TestCase):
    def setUp(self):
        self.bars = gld_bars([str(400 + index) for index in range(10)])

    def test_matching_sources_pass(self):
        self.assertEqual(cross_check_closes(self.bars, alpaca_from(self.bars[2:])), [])

    def test_stale_tiingo_and_missing_sessions_fail(self):
        problems = cross_check_closes(self.bars[:-1], alpaca_from(self.bars))
        self.assertTrue(problems[0].startswith("TIINGO_STALE"))
        gap = alpaca_from(self.bars[:5] + self.bars[6:])
        self.assertTrue(any(item.startswith("SESSION_DATES_DIFFER")
                            for item in cross_check_closes(self.bars, gap)))

    def test_close_disagreement_beyond_tolerance_fails(self):
        alpaca = alpaca_from(self.bars)
        alpaca[-1] = DailyBar(alpaca[-1].date, alpaca[-1].open,
                              alpaca[-1].close * Decimal("1.006"), 100)
        self.assertTrue(cross_check_closes(self.bars, alpaca)[0].startswith("CLOSE_MISMATCH"))
        alpaca[-1] = DailyBar(alpaca[-1].date, alpaca[-1].open,
                              self.bars[-1].close * Decimal("1.004"), 100)
        self.assertEqual(cross_check_closes(self.bars, alpaca), [])


class ReconciliationTests(unittest.TestCase):
    def test_ledger_matching_broker_passes(self):
        result = reconcile([ledger_order()], [Position("GLD", 10)], [])
        self.assertTrue(result.ok)
        self.assertEqual(result.broker_shares, 10)

    def test_manual_trades_and_foreign_orders_block(self):
        result = reconcile([], [Position("GLD", 3), Position("SPY", 1)],
                           [broker_order("manual-order")])
        self.assertFalse(result.ok)
        joined = " ".join(result.problems)
        for code in ("UNEXPECTED_POSITION", "POSITION_MISMATCH", "UNKNOWN_OPEN_ORDER"):
            self.assertIn(code, joined)

    def test_unresolved_and_duplicate_pending_orders_block(self):
        pending = [ledger_order(filled=0, status=SUBMITTING),
                   ledger_order(filled=0, status="new", client_id="tn-gld-b")]
        result = reconcile(pending, [], [broker_order("tn-gld-b")])
        joined = " ".join(result.problems)
        self.assertIn("UNRESOLVED_SUBMISSION", joined)
        self.assertIn("DUPLICATE_OPEN_ORDERS", joined)

    def test_missing_submission_resolves_only_when_definitive(self):
        record = ledger_order(filled=0, status=SUBMITTING)
        self.assertEqual(apply_broker_order(record, None, definitive=False).status, SUBMITTING)
        self.assertEqual(apply_broker_order(record, None, definitive=True).status, NOT_FOUND)
        filled = apply_broker_order(record, broker_order(status="filled", filled=10), True)
        self.assertEqual((filled.status, filled.filled_qty), ("filled", 10))
        with self.assertRaisesRegex(ValueError, "differs"):
            apply_broker_order(ledger_order(side="sell"), broker_order(), True)


class DrawdownAndSizingTests(unittest.TestCase):
    def test_drawdown_uses_highest_seen_equity(self):
        first = measure_drawdown(None, Decimal("100000"), Decimal("0.10"))
        self.assertEqual(first.peak_equity, Decimal("100000"))
        breach = measure_drawdown(first.peak_equity, Decimal("90000"), Decimal("0.10"))
        self.assertTrue(breach.breached)
        self.assertFalse(measure_drawdown(Decimal("100000"), Decimal("90001"),
                                          Decimal("0.10")).breached)

    def test_buy_is_sized_from_cash_with_limit_buffer(self):
        action = plan_order(1, 0, Decimal("100000"), Decimal("393.41"),
                            Decimal("0.50"), Decimal("0"))
        self.assertEqual(action.limit_price, Decimal("397.34"))
        self.assertEqual((action.action, action.qty), ("BUY", 125))
        self.assertLessEqual(action.qty * action.limit_price, Decimal("50000"))

    def test_sell_no_trade_and_rejection(self):
        self.assertEqual(plan_order(0, 7, Decimal(0), Decimal(400), Decimal("0.5"),
                                    Decimal(0)).qty, 7)
        self.assertEqual(plan_order(1, 7, Decimal(0), Decimal(400), Decimal("0.5"),
                                    Decimal(0)).action, "NO_TRADE")
        rejected = plan_order(1, 0, Decimal("100"), Decimal(400), Decimal("0.5"), Decimal(0))
        self.assertEqual((rejected.action, rejected.reason), ("REJECTED", "INSUFFICIENT_CASH"))


if __name__ == "__main__":
    unittest.main()
