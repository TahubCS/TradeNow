"""GLD paper-trading workflow: plan, human approval, submission, reconciliation, kill switch.

The strategy never talks to Alpaca. `plan` writes a proposed order to disk; only
`submit` with that plan's ID sends it. Every step checks the kill switch and
reconciles the local ledger with Alpaca first. State lives under the ignored
data/private/alpaca/ directory.
"""

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from .alpaca_paper import (Account, AlpacaError, BrokerOrder, Clock, DailyBar,
                           PaperOrder, Position)
from .equity import EquityConfig, simulate_equity
from .gld_research import parse_gld_csv
from .offline import CANDIDATES, evaluate_candidates
from .paper_rules import (SUBMITTING, LedgerOrder, Reconciliation, apply_broker_order,
                          cross_check_closes, measure_drawdown, plan_order, reconcile,
                          sma_signal)


STATE_DIR = Path(__file__).resolve().parent.parent / "data" / "private" / "alpaca"
# Alpaca's free data plan cannot query the most recent 15 minutes of SIP data.
DATA_LAG = timedelta(minutes=20)
CROSS_CHECK_CALENDAR_DAYS = 21
PLAN_ID = re.compile(r"[0-9a-f]{16}")


class PaperBlocked(ValueError):
    """A deliberate refusal to act; the code is machine-readable."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code


class BrokerClient(Protocol):
    def account(self) -> Account: ...
    def clock(self) -> Clock: ...
    def positions(self) -> list[Position]: ...
    def open_orders(self) -> list[BrokerOrder]: ...
    def gld_tradable(self) -> bool: ...
    def order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...
    def submit_gld(self, order: PaperOrder) -> BrokerOrder: ...
    def cancel_all_orders(self) -> int: ...
    def gld_daily_bars(self, start: date, end: datetime) -> list[DailyBar]: ...


@dataclass(frozen=True)
class DrawdownHalt:
    triggered_at: str
    peak_equity: Decimal
    equity: Decimal
    drawdown_pct: Decimal


@dataclass(frozen=True)
class PaperLedger:
    peak_equity: Decimal | None = None
    drawdown_halt: DrawdownHalt | None = None
    orders: tuple[LedgerOrder, ...] = ()


# ---------------------------------------------------------------- persistence

def _jsonable(value: object) -> object:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (Decimal, date)):
        return value.isoformat() if isinstance(value, date) else str(value)
    return value


def _stored_decimal(value: object, name: str, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    try:
        number = Decimal(value) if isinstance(value, str) else None
    except InvalidOperation:
        number = None
    if number is None or not number.is_finite():
        raise ValueError(f"Paper ledger has an invalid {name}")
    return number


def _order_from_json(item: object) -> LedgerOrder:
    if not isinstance(item, dict):
        raise ValueError("Paper ledger has a malformed order")
    try:
        order = LedgerOrder(
            client_order_id=str(item["client_order_id"]), plan_id=str(item["plan_id"]),
            session=date.fromisoformat(item["session"]), side=str(item["side"]),
            qty=item["qty"], status=str(item["status"]), filled_qty=item["filled_qty"],
            limit_price=_stored_decimal(item["limit_price"], "limit_price", True),
            filled_avg_price=_stored_decimal(item["filled_avg_price"],
                                             "filled_avg_price", True),
            broker_order_id=item["broker_order_id"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Paper ledger has a malformed order") from None
    if (order.side not in ("buy", "sell") or type(order.qty) is not int
            or type(order.filled_qty) is not int or not 0 <= order.filled_qty <= order.qty
            or not (order.broker_order_id is None or isinstance(order.broker_order_id, str))):
        raise ValueError("Paper ledger has an invalid order")
    return order


class PaperStore:
    def __init__(self, directory: Path = STATE_DIR):
        self.directory = directory
        self.ledger_path = directory / "ledger.json"
        self.kill_path = directory / "kill_switch.json"
        self.plans_dir = directory / "plans"

    def _write(self, path: Path, data: object) -> None:
        """Replace atomically so a crash never leaves a half-written state file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(_jsonable(data), indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def load_ledger(self) -> PaperLedger:
        if not self.ledger_path.exists():
            return PaperLedger()
        try:
            raw = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Paper ledger is unreadable") from None
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError("Paper ledger has an unsupported format")
        halt = raw.get("drawdown_halt")
        if halt is not None:
            if not isinstance(halt, dict) or not isinstance(halt.get("triggered_at"), str):
                raise ValueError("Paper ledger has a malformed drawdown halt")
            halt = DrawdownHalt(halt["triggered_at"],
                                _stored_decimal(halt.get("peak_equity"), "peak_equity"),
                                _stored_decimal(halt.get("equity"), "equity"),
                                _stored_decimal(halt.get("drawdown_pct"), "drawdown_pct"))
        orders = raw.get("orders")
        if not isinstance(orders, list):
            raise ValueError("Paper ledger has malformed orders")
        parsed = tuple(_order_from_json(item) for item in orders)
        if len({order.client_order_id for order in parsed}) != len(parsed):
            raise ValueError("Paper ledger repeats a client order ID")
        return PaperLedger(_stored_decimal(raw.get("peak_equity"), "peak_equity", True),
                           halt, parsed)

    def save_ledger(self, ledger: PaperLedger) -> None:
        self._write(self.ledger_path, {
            "schema_version": 1, "peak_equity": ledger.peak_equity,
            "drawdown_halt": None if ledger.drawdown_halt is None else asdict(ledger.drawdown_halt),
            "orders": [asdict(order) for order in ledger.orders]})

    def kill_switch(self) -> dict | None:
        """An unreadable kill-switch file counts as engaged (fail closed)."""
        if not self.kill_path.exists():
            return None
        try:
            raw = json.loads(self.kill_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"engaged_at": None, "reason": "kill switch file is unreadable"}
        return raw if isinstance(raw, dict) else {"engaged_at": None,
                                                  "reason": "kill switch file is malformed"}

    def engage_kill_switch(self, reason: str) -> None:
        if self.kill_switch() is None:
            self._write(self.kill_path, {"engaged_at": datetime.now(timezone.utc).isoformat(),
                                         "reason": reason[:500]})

    def clear_kill_switch(self) -> None:
        self.kill_path.unlink(missing_ok=True)

    def save_plan(self, plan: dict) -> Path:
        path = self.plans_dir / f"{plan['plan_id']}.json"
        self._write(path, plan)
        return path

    def load_plan(self, plan_id: str) -> dict:
        if not PLAN_ID.fullmatch(plan_id):
            raise PaperBlocked("UNKNOWN_PLAN", "plan IDs are 16 lowercase hex characters")
        path = self.plans_dir / f"{plan_id}.json"
        if not path.exists():
            raise PaperBlocked("UNKNOWN_PLAN", f"no saved plan {plan_id}")
        plan = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(plan, dict) or _plan_id(plan) != plan_id:
            raise PaperBlocked("PLAN_TAMPERED", "the saved plan does not match its ID")
        order = plan.get("order")
        if order and order.get("client_order_id") != _client_order_id(plan):
            raise PaperBlocked("PLAN_TAMPERED", "the saved order ID does not match the plan")
        return plan

    def latest_plan(self) -> dict | None:
        """The most recently written plan, verified like any plan being submitted."""
        paths = [path for path in self.plans_dir.glob("*.json")
                 if PLAN_ID.fullmatch(path.stem)] if self.plans_dir.exists() else []
        if not paths:
            return None
        return self.load_plan(max(paths, key=lambda path: path.stat().st_mtime).stem)


def _client_order_id(plan: dict) -> str:
    session = datetime.fromisoformat(plan["session_open"]).date()
    return f"tn-gld-{session:%Y%m%d}-{plan['order']['side']}-{plan['plan_id']}"


def _plan_id(plan: dict) -> str:
    """Hash of the plan's decision content; the order ID is derived from it."""
    body = {key: value for key, value in plan.items() if key != "plan_id"}
    if body.get("order"):
        body["order"] = {key: value for key, value in body["order"].items()
                         if key != "client_order_id"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


# ------------------------------------------------------------------- checks

def _require_no_kill_switch(store: PaperStore) -> None:
    kill = store.kill_switch()
    if kill is not None:
        raise PaperBlocked("KILL_SWITCH_ENGAGED",
                           f"{kill.get('reason')}; run paper-resume after review")


def _preflight(client: BrokerClient) -> tuple[Account, Clock]:
    account = client.account()
    if not account.is_paper_account:
        raise PaperBlocked("NOT_PAPER_ACCOUNT", "Alpaca did not report a paper account")
    if (account.status != "ACTIVE" or account.trading_blocked
            or account.currency != "USD"):
        raise PaperBlocked("ACCOUNT_NOT_TRADABLE", f"account status {account.status}")
    if account.cash < 0:
        raise PaperBlocked("MARGIN_IN_USE", "negative cash means margin is borrowed")
    if not client.gld_tradable():
        raise PaperBlocked("GLD_NOT_TRADABLE", "Alpaca reports GLD as not tradable")
    return account, client.clock()


def _sync(client: BrokerClient, ledger: PaperLedger) -> PaperLedger:
    """Refresh every unfinished ledger order from Alpaca by its client ID."""
    orders = []
    for order in ledger.orders:
        if not order.is_final:
            broker = client.order_by_client_id(order.client_order_id)
            order = apply_broker_order(order, broker, definitive=order.status == SUBMITTING)
        orders.append(order)
    return replace(ledger, orders=tuple(orders))


def _reconciled(client: BrokerClient, store: PaperStore,
                engage_on_mismatch: bool) -> tuple[PaperLedger, Reconciliation]:
    ledger = _sync(client, store.load_ledger())
    result = reconcile(list(ledger.orders), client.positions(), client.open_orders())
    if engage_on_mismatch:
        store.save_ledger(ledger)
        if not result.ok:
            store.engage_kill_switch("RECONCILIATION: " + "; ".join(result.problems))
            raise PaperBlocked("RECONCILIATION_FAILED", "; ".join(result.problems)
                               + " (kill switch engaged)")
    return ledger, result


def _send(client: BrokerClient, store: PaperStore, ledger: PaperLedger,
          record: LedgerOrder, order: PaperOrder) -> dict:
    """Record the order before sending so a crash or timeout cannot cause a resend."""
    ledger = replace(ledger, orders=ledger.orders + (record,))
    store.save_ledger(ledger)
    try:
        broker = client.submit_gld(order)
        outcome = "SUBMITTED"
    except AlpacaError as error:
        definitive = error.status is not None and 400 <= error.status < 500
        try:
            broker = client.order_by_client_id(record.client_order_id)
        except AlpacaError:
            broker, definitive = None, False
        updated = apply_broker_order(record, broker, definitive=definitive)
        store.save_ledger(replace(ledger, orders=ledger.orders[:-1] + (updated,)))
        if broker is None:
            raise PaperBlocked("SUBMIT_FAILED", f"{error}; ledger status {updated.status}"
                               " (the next command resolves it by client order ID)") from None
        outcome = "SUBMITTED_AFTER_ERROR"
    updated = apply_broker_order(record, broker, definitive=True)
    store.save_ledger(replace(ledger, orders=ledger.orders[:-1] + (updated,)))
    return {"result": outcome, "order": asdict(updated)}


# ----------------------------------------------------------------- commands

def paper_status(client: BrokerClient, store: PaperStore) -> dict:
    """Read-only view; writes nothing and never engages the kill switch."""
    account, clock = client.account(), client.clock()
    ledger, result = _reconciled(client, store, engage_on_mismatch=False)
    drawdown = measure_drawdown(ledger.peak_equity, account.equity,
                                EquityConfig().max_drawdown_fraction)
    return _jsonable({
        "mode": "alpaca_paper", "kill_switch": store.kill_switch(),
        "account": {"paper": account.is_paper_account, "status": account.status,
                    "cash": account.cash, "equity": account.equity,
                    "trading_blocked": account.trading_blocked},
        "clock": {"timestamp": clock.timestamp.isoformat(), "is_open": clock.is_open,
                  "next_open": clock.next_open.isoformat(),
                  "next_close": clock.next_close.isoformat()},
        "reconciliation": {"ok": result.ok, "ledger_shares": result.expected_shares,
                           "alpaca_shares": result.broker_shares,
                           "open_orders": result.open_orders, "problems": result.problems},
        "drawdown": {"peak_equity": ledger.peak_equity,
                     "current_pct": (drawdown.fraction * 100).quantize(Decimal("0.001")),
                     "halt": None if ledger.drawdown_halt is None
                     else asdict(ledger.drawdown_halt)},
        "recent_orders": [asdict(order) for order in ledger.orders[-5:]]})


def paper_plan(client: BrokerClient, store: PaperStore, source_bytes: bytes,
               source_name: str, config: EquityConfig = EquityConfig()) -> dict:
    """Decide tomorrow's GLD order from today's close; never sends it."""
    _require_no_kill_switch(store)
    account, clock = _preflight(client)
    if clock.is_open:
        raise PaperBlocked("MARKET_OPEN", "plan after the close and before the next open")
    ledger, reconciliation = _reconciled(client, store, engage_on_mismatch=True)
    if any(not order.is_final for order in ledger.orders):
        return {"action": "WAIT", "reason": "an earlier paper order is still open"}

    bars = parse_gld_csv(source_bytes, config)
    _, _, selected, _, _ = evaluate_candidates(bars, config, simulate_equity)
    last = bars[-1]
    session = clock.next_open.date()
    if last.date >= session:
        raise PaperBlocked("DATA_AHEAD_OF_SESSION", f"GLD data ends {last.date}")
    alpaca_bars = client.gld_daily_bars(last.date - timedelta(days=CROSS_CHECK_CALENDAR_DAYS),
                                        clock.timestamp - DATA_LAG)
    problems = cross_check_closes(bars, alpaca_bars)
    if problems:
        raise PaperBlocked("DATA_CHECK_FAILED", "; ".join(problems))

    chosen = next((item for item in CANDIDATES if item[0] == selected), None)
    signal = None if chosen is None else sma_signal(bars, chosen[1], chosen[2])
    drawdown = measure_drawdown(ledger.peak_equity, account.equity,
                                config.max_drawdown_fraction)
    halt = ledger.drawdown_halt
    if halt is None and drawdown.breached:
        halt = DrawdownHalt(clock.timestamp.isoformat(), drawdown.peak_equity,
                            drawdown.equity, (drawdown.fraction * 100).quantize(Decimal("0.001")))
    target = signal.strategy_target if signal is not None and halt is None else 0
    planned = plan_order(target, reconciliation.broker_shares, account.cash, last.close,
                         config.max_position_fraction, config.commission_per_order)
    reason = ("DRAWDOWN_EXIT" if planned.action == "SELL" and halt is not None else
              "SELECTION_GATE" if planned.action == "SELL" and signal is None else
              planned.reason)
    store.save_ledger(replace(ledger, peak_equity=drawdown.peak_equity, drawdown_halt=halt))

    plan = {
        "schema_version": 1, "created_at": clock.timestamp.isoformat(),
        "session_open": clock.next_open.isoformat(),
        "data": {"source_name": source_name,
                 "sha256": hashlib.sha256(source_bytes).hexdigest(),
                 "last_date": last.date.isoformat(), "reference_close": str(last.close),
                 "alpaca_sip_sessions_compared": len(alpaca_bars)},
        "selected_hypothesis": selected,
        "signal": None if signal is None else {
            "fast_window": chosen[1], "slow_window": chosen[2],
            "fast_sma": str(signal.fast_sma), "slow_sma": str(signal.slow_sma),
            "strategy_target": signal.strategy_target},
        "account": {"cash": str(account.cash), "equity": str(account.equity)},
        "drawdown": {"peak_equity": str(drawdown.peak_equity),
                     "current_pct": str((drawdown.fraction * 100).quantize(Decimal("0.001"))),
                     "threshold_pct": str(config.max_drawdown_fraction * 100),
                     "halted": halt is not None},
        "position_before": reconciliation.broker_shares, "target": target,
        "action": planned.action, "reason": reason,
        "order": None if planned.action not in ("BUY", "SELL") else {
            "side": planned.action.lower(), "qty": planned.qty,
            "type": "market" if planned.limit_price is None else "limit",
            "limit_price": None if planned.limit_price is None else str(planned.limit_price),
            "time_in_force": "day"},
    }
    plan["plan_id"] = _plan_id(plan)
    if plan["order"]:
        plan["order"]["client_order_id"] = _client_order_id(plan)
    path = store.save_plan(plan)
    return {**plan, "plan_file": str(path),
            "approve_with": (f"python -m tradenow paper-submit --approve {plan['plan_id']}"
                             if plan["order"] else None)}


def paper_submit(client: BrokerClient, store: PaperStore, plan_id: str) -> dict:
    """Send one approved plan's order, at most once, before its session opens."""
    _require_no_kill_switch(store)
    plan = store.load_plan(plan_id)
    details = plan.get("order")
    if not details:
        raise PaperBlocked("NOTHING_TO_SUBMIT", f"plan action is {plan.get('action')}")
    account, clock = _preflight(client)
    if clock.is_open or clock.next_open.isoformat() != plan["session_open"]:
        raise PaperBlocked("PLAN_EXPIRED", "plans are valid only until their session opens")
    ledger, reconciliation = _reconciled(client, store, engage_on_mismatch=True)

    client_order_id = details["client_order_id"]
    existing = next((order for order in ledger.orders
                     if order.client_order_id == client_order_id), None)
    if existing is not None:
        return {"result": "ALREADY_SUBMITTED", "order": _jsonable(asdict(existing))}
    if any(not order.is_final for order in ledger.orders):
        raise PaperBlocked("ORDER_PENDING", "another paper order is still open")
    session = clock.next_open.date()
    if any(order.session == session and order.filled_qty for order in ledger.orders):
        raise PaperBlocked("SESSION_ALREADY_TRADED", f"an order already filled for {session}")
    if reconciliation.broker_shares != plan["position_before"]:
        raise PaperBlocked("POSITION_CHANGED", "position differs from the plan; plan again")
    limit = None if details["limit_price"] is None else Decimal(details["limit_price"])
    order = PaperOrder(client_order_id, details["side"], details["qty"], limit)
    if order.side == "buy":
        if ledger.drawdown_halt is not None:
            raise PaperBlocked("DRAWDOWN_HALT", "new entries are blocked")
        if account.cash < order.qty * limit + EquityConfig().commission_per_order:
            raise PaperBlocked("INSUFFICIENT_CASH", "cash no longer covers the planned buy")
    record = LedgerOrder(client_order_id, plan_id, session, order.side, order.qty,
                         limit, SUBMITTING)
    return _jsonable(_send(client, store, ledger, record, order))


def paper_halt(client: BrokerClient, store: PaperStore, reason: str,
               flatten: bool) -> dict:
    """Engage the kill switch locally first, then cancel orders and optionally sell."""
    store.engage_kill_switch(reason)
    result: dict = {"kill_switch": "ENGAGED", "reason": reason}
    try:
        result["cancel_requests"] = client.cancel_all_orders()
        if flatten:
            _, clock = _preflight(client)
            ledger = _sync(client, store.load_ledger())
            store.save_ledger(ledger)
            shares = next((item.shares for item in client.positions()
                           if item.symbol == "GLD"), 0)
            if shares:
                client_order_id = f"tn-gld-flatten-{clock.timestamp:%Y%m%d%H%M%S}"
                order = PaperOrder(client_order_id, "sell", shares)
                record = LedgerOrder(client_order_id, "kill-switch", clock.next_open.date()
                                     if not clock.is_open else clock.timestamp.date(),
                                     "sell", shares, None, SUBMITTING)
                result["flatten"] = _send(client, store, ledger, record, order)
            else:
                result["flatten"] = {"result": "NO_GLD_POSITION"}
    except (AlpacaError, PaperBlocked) as error:
        result["error"] = str(error)
    return _jsonable(result)


def paper_resume(client: BrokerClient, store: PaperStore) -> dict:
    """Clear the kill switch only when Alpaca and the ledger agree."""
    if store.kill_switch() is None:
        return {"kill_switch": "NOT_ENGAGED"}
    _preflight(client)
    ledger, result = _reconciled(client, store, engage_on_mismatch=False)
    if not result.ok:
        raise PaperBlocked("RECONCILIATION_FAILED", "; ".join(result.problems))
    store.save_ledger(ledger)
    store.clear_kill_switch()
    return _jsonable({"kill_switch": "CLEARED", "gld_shares": result.broker_shares,
                      "drawdown_halt": None if ledger.drawdown_halt is None
                      else asdict(ledger.drawdown_halt)})
