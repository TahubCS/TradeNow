import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from tradenow.execution_quality import (
    SessionReference,
    cost_bps,
    order_quality,
    outcome,
    summarize_quality,
    summarize_runs,
)
from tradenow.paper_rules import NOT_FOUND, SUBMITTING, LedgerOrder


OPEN_TIME = datetime(2025, 12, 9, 9, 30, tzinfo=timezone(timedelta(hours=-5)))
SLIPPAGE = Decimal("0.01")


def order(side: str = "buy", status: str = "filled", filled: int = 10,
          price: str | None = "300.30", limit: str | None = "303.00") -> LedgerOrder:
    return LedgerOrder(f"tn-gld-20251209-{side}", "0123456789abcdef", date(2025, 12, 9), side,
                       10, None if side == "sell" else Decimal(limit) if limit else None,
                       status, filled, None if price is None else Decimal(price), "b1",
                       reference_close=Decimal("300.00"), sent_at=OPEN_TIME - timedelta(hours=14),
                       submitted_at=OPEN_TIME - timedelta(hours=14),
                       filled_at=OPEN_TIME + timedelta(seconds=1, milliseconds=250))


class ExecutionQualityTests(unittest.TestCase):
    def test_costs_are_positive_when_the_account_does_worse(self):
        self.assertEqual(cost_bps("buy", Decimal("300.30"), Decimal("300")), Decimal("10.00"))
        self.assertEqual(cost_bps("sell", Decimal("299.70"), Decimal("300")), Decimal("10.00"))
        self.assertEqual(cost_bps("sell", Decimal("300.30"), Decimal("300")), Decimal("-10.00"))

    def test_outcomes_cover_every_final_state(self):
        self.assertEqual(outcome(order()), "FILLED")
        self.assertEqual(outcome(order(filled=4)), "PARTIAL")
        self.assertEqual(outcome(order(status="expired", filled=0, price=None)), "UNFILLED")
        self.assertEqual(outcome(order(status="rejected", filled=0, price=None)), "REJECTED")
        self.assertEqual(outcome(order(status=NOT_FOUND, filled=0, price=None)), "NOT_RECEIVED")
        self.assertEqual(outcome(order(status=SUBMITTING, filled=0, price=None)), "OPEN")
        self.assertEqual(outcome(order(status="accepted", filled=0, price=None)), "OPEN")

    def test_buy_is_compared_with_plan_open_and_simulated_fill(self):
        row = order_quality(order(), SessionReference(Decimal("300.20"), OPEN_TIME), SLIPPAGE)
        self.assertEqual(row["reference"]["simulated"], Decimal("300.21"))
        self.assertEqual(row["slippage_bps"]["plan"], Decimal("10.00"))
        self.assertEqual(row["slippage_bps"]["open"], Decimal("3.33"))
        self.assertEqual(row["cost_vs_simulation"], Decimal("0.90"))
        self.assertEqual(row["latency_s"]["open_to_fill"], Decimal("1.250"))
        self.assertFalse(row["limit_below_open"])

    def test_gap_above_the_buy_limit_is_flagged(self):
        missed = order(status="expired", filled=0, price=None, limit="301.00")
        row = order_quality(missed, SessionReference(Decimal("305.00"), OPEN_TIME), SLIPPAGE)
        self.assertTrue(row["limit_below_open"])
        self.assertEqual(row["slippage_bps"], {})
        self.assertIsNone(row["cost_vs_simulation"])

    def test_summary_rates_and_means(self):
        session = SessionReference(Decimal("300.20"), OPEN_TIME)
        rows = [order_quality(item, session, SLIPPAGE) for item in (
            order(),
            order(side="sell", price="300.00"),
            order(status="rejected", filled=0, price=None),
            order(status="accepted", filled=0, price=None),
        )]
        summary = summarize_quality(rows)
        self.assertEqual(summary["orders"], 4)
        self.assertEqual(summary["outcomes"],
                         {"FILLED": 2, "OPEN": 1, "REJECTED": 1})
        self.assertEqual(summary["fill_rate_pct"], Decimal("66.67"))
        self.assertEqual(summary["rejection_rate_pct"], Decimal("33.33"))
        # Buy: +3.33 bps vs open; sell at 300.00 against a 300.20 open: +6.66 bps.
        self.assertEqual(summary["mean_slippage_bps"]["open"], Decimal("5.00"))
        self.assertEqual(summary["total_cost_vs_simulation"], Decimal("2.80"))
        self.assertEqual(summary["orders_awaiting_open_price"], 0)

    def test_fill_without_session_open_waits_for_next_import(self):
        row = order_quality(order(), SessionReference(), SLIPPAGE)
        self.assertEqual(set(row["slippage_bps"]), {"plan"})
        self.assertEqual(summarize_quality([row])["orders_awaiting_open_price"], 1)
        self.assertIsNone(summarize_quality([])["fill_rate_pct"])

    def test_run_summary_counts_blocks_and_repeat_submissions(self):
        runs = [{"outcome": "OK", "started_at": "a"},
                {"outcome": "BLOCKED", "code": "RECONCILIATION_FAILED", "started_at": "b"},
                {"outcome": "OK", "result": "ALREADY_SUBMITTED", "started_at": "c"}]
        summary = summarize_runs(runs)
        self.assertEqual(summary["outcomes"], {"BLOCKED": 1, "OK": 2})
        self.assertEqual(summary["reconciliation_failures"], 1)
        self.assertEqual(summary["repeat_submissions"], 1)
        self.assertEqual(summary["last_run_at"], "c")


if __name__ == "__main__":
    unittest.main()
