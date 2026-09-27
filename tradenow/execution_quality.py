"""Execution quality: how each paper fill compares with the plan and the backtest.

Each filled order is compared with three reference prices:

- plan: the close the plan was computed from (the price the signal saw),
- open: the session's actual opening price in the Tiingo history,
- simulated: what simulate_equity would have paid, the open plus or minus
  its per-share slippage.

Slippage is in basis points and signed so a positive number is a cost to the
account. Nothing here performs I/O.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .paper_rules import NOT_FOUND, LedgerOrder


BPS = Decimal("10000")
CENT = Decimal("0.01")
BENCHMARKS = ("plan", "open", "simulated")


@dataclass(frozen=True)
class SessionReference:
    """What the order's session looked like; any part may still be unknown."""
    open_price: Decimal | None = None
    open_time: datetime | None = None


def outcome(order: LedgerOrder) -> str:
    if not order.is_final:
        return "OPEN"
    if order.status == NOT_FOUND:
        return "NOT_RECEIVED"
    if order.status == "rejected":
        return "REJECTED"
    if order.filled_qty == order.qty:
        return "FILLED"
    return "PARTIAL" if order.filled_qty else "UNFILLED"


def cost_bps(side: str, fill: Decimal, reference: Decimal) -> Decimal:
    """Paying more on a buy, or receiving less on a sell, is a positive cost."""
    sign = 1 if side == "buy" else -1
    return (sign * (fill - reference) / reference * BPS).quantize(CENT)


def _seconds(start: datetime | None, end: datetime | None) -> Decimal | None:
    if start is None or end is None:
        return None
    return Decimal(str((end - start).total_seconds())).quantize(Decimal("0.001"))


def order_quality(order: LedgerOrder, session: SessionReference,
                  slippage_per_share: Decimal) -> dict:
    sign = 1 if order.side == "buy" else -1
    simulated = (None if session.open_price is None
                 else session.open_price + sign * slippage_per_share)
    row: dict = {
        "client_order_id": order.client_order_id, "plan_id": order.plan_id,
        "session": order.session, "side": order.side, "qty": order.qty,
        "status": order.status, "outcome": outcome(order), "filled_qty": order.filled_qty,
        "limit_price": order.limit_price, "fill_price": order.filled_avg_price,
        "reference": {"plan": order.reference_close, "open": session.open_price,
                      "simulated": simulated},
        # The simulator buys at any open; a paper buy's limit can be below it.
        "limit_below_open": (None if order.limit_price is None or session.open_price is None
                             else order.limit_price < session.open_price),
        "sent_at": order.sent_at, "submitted_at": order.submitted_at,
        "filled_at": order.filled_at,
        "latency_s": {"submit_to_fill": _seconds(order.submitted_at, order.filled_at),
                      "open_to_fill": _seconds(session.open_time, order.filled_at)},
        "slippage_bps": {}, "cost_vs_simulation": None,
    }
    fill = order.filled_avg_price
    if fill is not None and order.filled_qty:
        row["slippage_bps"] = {name: cost_bps(order.side, fill, price)
                               for name, price in row["reference"].items() if price is not None}
        if simulated is not None:
            row["cost_vs_simulation"] = (sign * (fill - simulated)
                                         * order.filled_qty).quantize(CENT)
    return row


def _mean(values: list[Decimal], places: str) -> Decimal | None:
    if not values:
        return None
    return (sum(values, Decimal(0)) / len(values)).quantize(Decimal(places))


def _pct(part: int, whole: int) -> Decimal | None:
    return None if not whole else (Decimal(part) * 100 / whole).quantize(CENT)


def summarize_quality(rows: list[dict]) -> dict:
    counts = Counter(row["outcome"] for row in rows)
    finished = len(rows) - counts["OPEN"]
    filled = [row for row in rows if row["slippage_bps"]]
    costs = [row["cost_vs_simulation"] for row in rows if row["cost_vs_simulation"] is not None]
    latencies = [row["latency_s"]["open_to_fill"] for row in rows
                 if row["latency_s"]["open_to_fill"] is not None]
    return {
        "orders": len(rows), "outcomes": dict(sorted(counts.items())),
        "fill_rate_pct": _pct(counts["FILLED"] + counts["PARTIAL"], finished),
        "rejection_rate_pct": _pct(counts["REJECTED"] + counts["NOT_RECEIVED"], finished),
        "partial_fills": counts["PARTIAL"],
        "limit_below_open": sum(1 for row in rows if row["limit_below_open"]),
        "mean_slippage_bps": {
            name: _mean([row["slippage_bps"][name] for row in filled
                         if name in row["slippage_bps"]], "0.01")
            for name in BENCHMARKS},
        "mean_open_to_fill_s": _mean(latencies, "0.001"),
        "total_cost_vs_simulation": (None if not costs
                                     else sum(costs, Decimal(0)).quantize(CENT)),
        "orders_awaiting_open_price": sum(1 for row in rows if row["filled_qty"]
                                          and row["reference"]["open"] is None),
    }


def summarize_runs(runs: list[dict]) -> dict:
    """Operational health from the run log: outcomes, block reasons, repeated submits."""
    outcomes = Counter(str(run.get("outcome")) for run in runs)
    codes = Counter(str(run["code"]) for run in runs if run.get("code"))
    return {"total": len(runs), "outcomes": dict(sorted(outcomes.items())),
            "blocked_codes": dict(sorted(codes.items())),
            "reconciliation_failures": codes["RECONCILIATION_FAILED"],
            "repeat_submissions": sum(1 for run in runs
                                      if run.get("result") == "ALREADY_SUBMITTED"),
            "last_run_at": runs[-1].get("started_at") if runs else None}
