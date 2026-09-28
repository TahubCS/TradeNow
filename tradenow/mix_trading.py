"""Fixed-mix mode in the Alpaca paper account (ADR-015): orchestration and state.

The evening run imports the close, cross-checks every symbol against Alpaca,
records equity, sends drawdown alerts, and, when a rebalance is due, plans
the orders with mix_rules.plan_rebalance: sells on one evening, buys from the
cash actually in the account on a later one. Orders are recorded before they
are sent and sent at most once. The morning run only reconciles and reports.

Any mismatch with Alpaca, a position or order this mode did not create, or
negative cash engages the kill switch shared with GLD mode. Nothing here can
reach a live account: PaperClient accepts only the paper endpoint.
"""

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from .alpaca_paper import (
    Account,
    AlpacaError,
    BrokerOrder,
    Clock,
    DailyBar,
    MixOrder,
    Position,
)
from .equity_types import EquityBar
from .mix_config import Mix
from .mix_rules import (
    MixOrderRecord,
    MixReconciliation,
    apply_broker,
    drawdown_alerts,
    plan_rebalance,
    quarter,
    rebalance_due,
    reconcile_mix,
)
from .paper_auto import Notices, run_lock
from .paper_rules import SUBMITTING, cross_check_closes
from .paper_trading import MODE_MARKER, PaperBlocked, PaperStore, _jsonable
from .risk_config import LoadedRisk
from .settings import SETTINGS


MIX_DIR = SETTINGS.data_dir / "mix"
LOCK_NAME = "mix-auto.lock"
CROSS_CHECK_DAYS = 21
WAITING_CODES = ("TIINGO_STALE",)
LEDGER_SCHEMA = 1


class MixBroker(Protocol):
    def account(self) -> Account: ...
    def clock(self) -> Clock: ...
    def positions(self) -> list[Position]: ...
    def open_orders(self) -> list[BrokerOrder]: ...
    def order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...
    def submit_mix(self, order: MixOrder) -> BrokerOrder: ...
    def asset_tradable(self, symbol: str) -> bool: ...
    def daily_history(self, symbol: str, start: date, end: date) -> list[DailyBar]: ...
    def cancel_all_orders(self) -> int: ...


# Recent raw closes by symbol, from the latest Tiingo imports.
CloseLoader = Callable[[list[str]], dict[str, dict[date, Decimal]]]
# Import every symbol through a date; returns a summary per symbol.
Importer = Callable[[list[str], date], dict]


def _decimal(value: object, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        raise ValueError(f"Mix ledger has an invalid {name}") from None
    if not number.is_finite():
        raise ValueError(f"Mix ledger has an invalid {name}")
    return number


def _record_from_json(item: object) -> MixOrderRecord:
    if not isinstance(item, dict):
        raise ValueError("Mix ledger has a malformed order")
    try:
        return MixOrderRecord(
            str(item["client_order_id"]), str(item["plan_id"]),
            date.fromisoformat(item["session"]), str(item["symbol"]), str(item["side"]),
            int(item["qty"]),
            None if item.get("limit_price") is None else _decimal(item["limit_price"],
                                                                   "limit price"),
            str(item["status"]), _decimal(item["reference_close"], "reference close"),
            int(item.get("filled_qty", 0)),
            None if item.get("filled_avg_price") is None
            else _decimal(item["filled_avg_price"], "fill price"),
            item.get("broker_order_id"),
            None if item.get("sent_at") is None else datetime.fromisoformat(item["sent_at"]),
            str(item.get("approval", "auto")))
    except (KeyError, TypeError, ValueError):
        raise ValueError("Mix ledger has a malformed order") from None


class MixStore:
    """Mix mode's own files, plus the account store's kill switch and mode marker."""

    def __init__(self, directory: Path = MIX_DIR, account: PaperStore | None = None):
        self.directory = directory
        self.account = account or PaperStore()
        self.ledger_path = directory / "ledger.json"
        self.state_path = directory / "state.json"
        self.plans_dir = directory / "plans"
        self.equity_path = directory / "equity_history.jsonl"

    def _write(self, path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(_jsonable(data), indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def load_ledger(self) -> tuple[MixOrderRecord, ...]:
        if not self.ledger_path.exists():
            return ()
        try:
            raw = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Mix ledger is unreadable") from None
        if not isinstance(raw, dict) or raw.get("schema_version") != LEDGER_SCHEMA:
            raise ValueError("Mix ledger has an unsupported format")
        orders = raw.get("orders")
        if not isinstance(orders, list):
            raise ValueError("Mix ledger has malformed orders")
        records = tuple(_record_from_json(item) for item in orders)
        if len({record.client_order_id for record in records}) != len(records):
            raise ValueError("Mix ledger repeats a client order ID")
        return records

    def save_ledger(self, orders: tuple[MixOrderRecord, ...]) -> None:
        self._write(self.ledger_path, {"schema_version": LEDGER_SCHEMA,
                                       "orders": [asdict(order) for order in orders]})

    def load_state(self) -> dict:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Mix state is unreadable") from None
        if not isinstance(raw, dict):
            raise ValueError("Mix state is malformed")
        return raw

    def save_state(self, state: dict) -> None:
        self._write(self.state_path, state)

    def save_plan(self, plan: dict) -> Path:
        path = self.plans_dir / f"{plan['plan_id']}.json"
        self._write(path, plan)
        return path

    def record_equity(self, day: date, equity: Decimal, mix_sha256: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.equity_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"date": day.isoformat(), "equity": str(equity),
                                     "mix_sha256": mix_sha256,
                                     "recorded_at": datetime.now(timezone.utc).isoformat()})
                         + "\n")

    def equity_history(self) -> list[tuple[date, Decimal]]:
        """One value per date; a later line for the same date wins."""
        if not self.equity_path.exists():
            return []
        by_date: dict[date, Decimal] = {}
        for line in self.equity_path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                by_date[date.fromisoformat(item["date"])] = _decimal(item["equity"], "equity")
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
        return sorted(by_date.items())

    # The kill switch and mode marker belong to the account, shared with GLD mode.
    def kill_switch(self) -> dict | None:
        return self.account.kill_switch()

    def engage_kill_switch(self, reason: str) -> None:
        self.account.engage_kill_switch(reason)

    @property
    def marker_path(self) -> Path:
        return self.account.directory / MODE_MARKER

    def mode_active(self) -> bool:
        return self.marker_path.exists()

    def activate(self, mix: Mix) -> None:
        self.account._write(self.marker_path, {
            "mode": "mix", "activated_at": datetime.now(timezone.utc).isoformat(),
            "mix_sha256": mix.sha256,
            "note": "GLD paper commands refuse while this file exists (ADR-015)"})


# ------------------------------------------------------------------ helpers

def _require_no_kill_switch(store: MixStore) -> None:
    kill = store.kill_switch()
    if kill is not None:
        raise PaperBlocked("KILL_SWITCH_ENGAGED",
                           f"{kill.get('reason')}; run mix-resume after review")


def _preflight(client: MixBroker, store: MixStore) -> tuple[Account, Clock]:
    account = client.account()
    if not account.is_paper_account:
        raise PaperBlocked("NOT_PAPER_ACCOUNT", "Alpaca did not report a paper account")
    if account.status != "ACTIVE" or account.trading_blocked or account.currency != "USD":
        raise PaperBlocked("ACCOUNT_NOT_TRADABLE", f"account status {account.status}")
    if account.cash < 0:
        store.engage_kill_switch("MARGIN_IN_USE: negative cash")
        raise PaperBlocked("MARGIN_IN_USE", "negative cash means margin is borrowed "
                           "(kill switch engaged)")
    return account, client.clock()


def _activate(client: MixBroker, store: MixStore, mix: Mix) -> bool:
    """The first run needs an empty account; it then marks the account as mix mode."""
    if store.mode_active():
        return False
    positions, orders = client.positions(), client.open_orders()
    if positions or orders:
        raise PaperBlocked("ACCOUNT_NOT_EMPTY",
                           f"{len(positions)} position(s) and {len(orders)} open order(s); "
                           "reset the Alpaca paper account before the first mix run")
    store.activate(mix)
    return True


def _sync(client: MixBroker, orders: tuple[MixOrderRecord, ...]) -> tuple[MixOrderRecord, ...]:
    return tuple(order if order.is_final else
                 apply_broker(order, client.order_by_client_id(order.client_order_id),
                              definitive=order.status == SUBMITTING)
                 for order in orders)


def _reconciled(client: MixBroker, store: MixStore, engage: bool = True
                ) -> tuple[tuple[MixOrderRecord, ...], MixReconciliation]:
    orders = _sync(client, store.load_ledger())
    result = reconcile_mix(orders, client.positions(), client.open_orders())
    if engage:
        store.save_ledger(orders)
        if not result.ok:
            store.engage_kill_switch("MIX RECONCILIATION: " + "; ".join(result.problems))
            raise PaperBlocked("RECONCILIATION_FAILED", "; ".join(result.problems)
                               + " (kill switch engaged)")
    return orders, result


def _latest_closes(client: MixBroker, symbols: list[str], closes: Mapping[str, Mapping],
                   clock: Clock) -> tuple[date, dict[str, Decimal], int]:
    """Every symbol's last close, all on one session, cross-checked against Alpaca."""
    missing = [symbol for symbol in symbols if not closes.get(symbol)]
    if missing:
        raise PaperBlocked("NO_DATA", f"no Tiingo closes for {', '.join(missing)}")
    last_dates = {symbol: max(closes[symbol]) for symbol in symbols}
    last = max(last_dates.values())
    behind = [symbol for symbol, day in last_dates.items() if day < last]
    if behind:
        raise PaperBlocked("TIINGO_STALE", f"{', '.join(behind)} end before {last}")
    if last >= clock.next_open.date():
        raise PaperBlocked("DATA_AHEAD_OF_SESSION", f"closes end {last}")
    compared = 0
    for symbol in symbols:
        recent = sorted(day for day in closes[symbol]
                        if day >= last - timedelta(days=CROSS_CHECK_DAYS))
        bars = [EquityBar(day, symbol, closes[symbol][day], closes[symbol][day],
                          closes[symbol][day], closes[symbol][day], 0) for day in recent]
        alpaca = client.daily_history(symbol, last - timedelta(days=CROSS_CHECK_DAYS),
                                      clock.timestamp.date())
        problems = cross_check_closes(bars, alpaca)
        if problems:
            code = "TIINGO_STALE" if any("TIINGO_STALE" in item for item in problems) \
                else "DATA_CHECK_FAILED"
            raise PaperBlocked(code, f"{symbol}: " + "; ".join(problems))
        compared += len(alpaca)
    return last, {symbol: closes[symbol][last] for symbol in symbols}, compared


def _plan_id(plan: dict) -> str:
    body = json.dumps({key: value for key, value in plan.items() if key != "plan_id"},
                      sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def _send(client: MixBroker, store: MixStore, orders: tuple[MixOrderRecord, ...],
          record: MixOrderRecord) -> tuple[tuple[MixOrderRecord, ...], str]:
    """Record the order before sending, so a crash or timeout cannot cause a resend."""
    orders = orders + (record,)
    store.save_ledger(orders)
    order = MixOrder(record.client_order_id, record.symbol, record.side, record.qty,
                     record.limit_price)
    try:
        broker = client.submit_mix(order)
        outcome = "SUBMITTED"
    except AlpacaError as error:
        definitive = error.status is not None and 400 <= error.status < 500
        try:
            found = client.order_by_client_id(record.client_order_id)
        except AlpacaError:
            found, definitive = None, False
        updated = apply_broker(record, found, definitive=definitive)
        store.save_ledger(orders[:-1] + (updated,))
        if found is None:
            raise PaperBlocked("SUBMIT_FAILED", f"{record.symbol}: {error}; ledger status "
                               f"{updated.status} (the next run resolves it)") from None
        broker, outcome = found, "SUBMITTED_AFTER_ERROR"
    orders = orders[:-1] + (apply_broker(record, broker, definitive=True),)
    store.save_ledger(orders)
    return orders, outcome


def _describe(action: dict) -> str:
    price = f" limit ${action['limit_price']}" if action.get("limit_price") else " at market"
    return f"{action['side'].upper()} {action['qty']} {action['symbol']}{price}"


# ------------------------------------------------------------------ commands

def mix_evening(client: MixBroker, store: MixStore, mix: Mix, risk: LoadedRisk,
                import_through: Importer, load_closes: CloseLoader, notices: Notices,
                dry_run: bool = False) -> dict:
    """Plan (and, with auto_submit, send) tonight's orders. Never trades while open."""
    _require_no_kill_switch(store)
    account, clock = _preflight(client, store)
    if clock.is_open:
        raise PaperBlocked("MARKET_OPEN", "plan after the close and before the next open")
    activated = _activate(client, store, mix)
    orders, reconciliation = _reconciled(client, store)
    if any(not order.is_final for order in orders):
        return {"result": "WAIT", "reason": "earlier mix orders are still open"}
    symbols = list(mix.targets) + [symbol for symbol in reconciliation.broker
                                   if symbol not in mix.targets]
    untradable = [symbol for symbol in mix.targets if not client.asset_tradable(symbol)]
    if untradable:
        raise PaperBlocked("NOT_TRADABLE", f"Alpaca cannot trade {', '.join(untradable)}")
    end = min(clock.timestamp.date(), date.today())
    imported = import_through(symbols, end)
    try:
        last, closes, compared = _latest_closes(client, symbols, load_closes(symbols), clock)
    except PaperBlocked as error:
        if error.code in WAITING_CODES:
            return {"result": "WAITING_FOR_DATA", "detail": str(error), "import": imported}
        raise

    state = store.load_state()
    store.record_equity(last, account.equity, mix.sha256)
    alerts = drawdown_alerts(None if state.get("peak_equity") is None
                             else Decimal(state["peak_equity"]),
                             Decimal(state.get("alerted_drawdown", "0")), account.equity)
    for level in alerts.crossed:
        notices.send(f"drawdown:{level}:{alerts.peak}", "Mix portfolio drawdown",
                     f"Down {(alerts.fraction * 100).quantize(Decimal('0.1'))}% from its peak "
                     f"of ${alerts.peak}. Information only; nothing is sold automatically.",
                     once_per_day=False)
    limit = risk.config.daily_loss_limit_fraction
    if account.last_equity and (account.last_equity - account.equity) / account.last_equity >= limit:
        notices.send("daily-loss", "Mix portfolio daily loss",
                     f"Equity fell from ${account.last_equity} to ${account.equity} today. "
                     "Information only.")
    state.update({"peak_equity": str(alerts.peak), "alerted_drawdown": str(alerts.alerted),
                  "last_close_date": last.isoformat()})
    result: dict[str, Any] = {"import": imported, "last_close": last.isoformat(),
                              "equity": str(account.equity), "cash": str(account.cash),
                              "activated": activated,
                              "drawdown_pct": str((alerts.fraction * 100).quantize(
                                  Decimal("0.001")))}
    if not rebalance_due(state.get("checked_quarter"), state.get("checked_mix"),
                         bool(state.get("rebalancing")), mix.sha256, last):
        store.save_state(state)
        return {**result, "result": "NO_REBALANCE_DUE", "quarter": quarter(last)}

    rebalance = plan_rebalance(mix.targets, reconciliation.broker, closes, account.cash,
                               risk.config.buy_limit_buffer)
    state.update({"checked_quarter": quarter(last), "checked_mix": mix.sha256,
                  "rebalancing": rebalance.phase != "NONE"})
    plan: dict[str, Any] = {
        "schema_version": 1, "created_at": clock.timestamp.isoformat(),
        "session_open": clock.next_open.isoformat(), "last_close": last.isoformat(),
        "mix": {symbol: str(weight) for symbol, weight in mix.targets.items()},
        "mix_sha256": mix.sha256, "risk_sha256": risk.sha256,
        "account": {"cash": str(account.cash), "equity": str(account.equity)},
        "holdings": reconciliation.broker, "closes": {s: str(v) for s, v in closes.items()},
        "alpaca_sessions_compared": compared,
        "phase": rebalance.phase, "note": rebalance.note,
        "weights": {s: str(v.quantize(Decimal("0.0001"))) for s, v in rebalance.weights.items()},
        "drift": {s: str(v.quantize(Decimal("0.0001"))) for s, v in rebalance.drift.items()},
        "orders": [{"symbol": item.symbol, "side": item.side, "qty": item.qty,
                    "limit_price": None if item.limit_price is None else str(item.limit_price),
                    "type": "market" if item.limit_price is None else "limit",
                    "time_in_force": "day", "reason": item.reason}
                   for item in rebalance.actions]}
    plan["plan_id"] = _plan_id(plan)
    for number, order in enumerate(plan["orders"]):
        order["client_order_id"] = f"tn-mix-{plan['plan_id']}-{number}"
    path = store.save_plan(plan)
    result.update({"result": f"REBALANCE_{rebalance.phase}", "plan_id": plan["plan_id"],
                   "plan_file": str(path), "orders": plan["orders"], "note": rebalance.note})
    if plan["orders"]:
        if risk.config.auto_submit and not dry_run:
            sent = []
            for order in plan["orders"]:
                record = MixOrderRecord(
                    order["client_order_id"], plan["plan_id"], clock.next_open.date(),
                    order["symbol"], order["side"], order["qty"],
                    None if order["limit_price"] is None else Decimal(order["limit_price"]),
                    SUBMITTING, closes[order["symbol"]],
                    sent_at=datetime.now(timezone.utc))
                orders, outcome = _send(client, store, orders, record)
                sent.append({"client_order_id": record.client_order_id, "result": outcome})
                notices.send(f"sent:{record.client_order_id}", "Mix order sent",
                             f"{_describe(order)} ({order['reason']})", once_per_day=False)
            result["submission"] = sent
        else:
            result["submission"] = "NOT_SENT_DRY_RUN"
            notices.send(f"dry:{plan['plan_id']}", "Mix dry run",
                         "Would send: " + "; ".join(_describe(order) for order in plan["orders"])
                         + " (auto_submit is off)")
    store.save_state(state)
    return result


def mix_morning(client: MixBroker, store: MixStore, notices: Notices) -> dict:
    """Reconcile and report orders that finished since the evening; never trades."""
    _require_no_kill_switch(store)
    _preflight(client, store)
    before = {order.client_order_id: order for order in store.load_ledger()}
    orders, reconciliation = _reconciled(client, store)
    finished = []
    for order in orders:
        earlier = before.get(order.client_order_id)
        if earlier is None or earlier.is_final or not order.is_final:
            continue
        finished.append(order.client_order_id)
        message = (f"{order.side.upper()} {order.filled_qty} {order.symbol} filled at "
                   f"${order.filled_avg_price}" if order.filled_qty else
                   f"{order.side.upper()} {order.qty} {order.symbol} ended {order.status}, "
                   "unfilled")
        notices.send(f"order:{order.client_order_id}", "Mix order finished", message,
                     once_per_day=False)
    return {"result": "CHECKED", "finished_orders": finished, "holdings": reconciliation.broker}


def mix_auto(client: MixBroker, store: MixStore, mix: Mix, risk: LoadedRisk,
             import_through: Importer, load_closes: CloseLoader,
             notify: Callable[[str, str], object], dry_run: bool = False,
             check_only: bool = False) -> dict:
    """One scheduled run, one at a time. Refusals raise PaperBlocked and notify."""
    notices = Notices(store, notify, date.today())
    with run_lock(store.directory / LOCK_NAME):
        try:
            result = (mix_morning(client, store, notices) if check_only else
                      mix_evening(client, store, mix, risk, import_through, load_closes,
                                  notices, dry_run))
        except PaperBlocked as error:
            if error.code not in WAITING_CODES:
                notices.send(f"blocked:{error.code}", "Mix run blocked", str(error))
            raise
        except (OSError, ValueError) as error:
            notices.send(f"error:{type(error).__name__}", "Mix run error", str(error))
            raise
    result["mode"] = "dry_run" if dry_run or not risk.config.auto_submit else "auto_submit"
    store._write(store.directory / "last_auto.json",
                 {**result, "finished_at": datetime.now(timezone.utc).isoformat()})
    return result


def mix_status(client: MixBroker, store: MixStore, mix: Mix) -> dict:
    """Read-only: writes nothing and never engages the kill switch."""
    account = client.account()
    _, reconciliation = _reconciled(client, store, engage=False)
    return {"mode_active": store.mode_active(), "kill_switch": store.kill_switch(),
            "account": {"equity": str(account.equity), "cash": str(account.cash)},
            "mix": {symbol: str(weight) for symbol, weight in mix.targets.items()},
            "holdings": reconciliation.broker, "reconciliation_ok": reconciliation.ok,
            "problems": list(reconciliation.problems), "state": store.load_state()}


def mix_halt(client: MixBroker, store: MixStore, reason: str) -> dict:
    """Cancel open orders and engage the kill switch. Positions are kept."""
    store.engage_kill_switch(f"MIX HALT: {reason}")
    return {"result": "HALTED", "cancelled_orders": client.cancel_all_orders(),
            "kill_switch": store.kill_switch()}


def mix_resume(client: MixBroker, store: MixStore) -> dict:
    """Clear the kill switch only when the ledger and Alpaca agree again."""
    orders, reconciliation = _reconciled(client, store, engage=False)
    if not reconciliation.ok:
        raise PaperBlocked("RECONCILIATION_FAILED", "; ".join(reconciliation.problems))
    if client.account().cash < 0:
        raise PaperBlocked("MARGIN_IN_USE", "negative cash")
    store.save_ledger(orders)
    store.account.clear_kill_switch()
    return {"result": "RESUMED", "holdings": reconciliation.broker}


@dataclass(frozen=True)
class _Fill:
    side: str
    qty: int
    price: Decimal
    reference: Decimal


def mix_report(store: MixStore) -> dict:
    """What happened in paper: equity, drawdown, orders, and fill costs."""
    history = store.equity_history()
    orders = store.load_ledger()
    fills = [_Fill(order.side, order.filled_qty, order.filled_avg_price, order.reference_close)
             for order in orders if order.filled_qty and order.filled_avg_price is not None]
    costs = [((fill.price - fill.reference) if fill.side == "buy"
              else (fill.reference - fill.price)) / fill.reference * 10000 for fill in fills]
    peak, worst = None, Decimal(0)
    for _, equity in history:
        peak = equity if peak is None else max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    return {
        "sessions": len(history),
        "first": None if not history else {"date": history[0][0].isoformat(),
                                           "equity": str(history[0][1])},
        "last": None if not history else {"date": history[-1][0].isoformat(),
                                          "equity": str(history[-1][1])},
        "return_pct": None if len(history) < 2 else
        str(((history[-1][1] / history[0][1] - 1) * 100).quantize(Decimal("0.001"))),
        "max_drawdown_pct": str((worst * 100).quantize(Decimal("0.001"))),
        "orders": len(orders), "fills": len(fills),
        "unfilled": sum(order.is_final and not order.filled_qty for order in orders),
        "mean_fill_cost_bps": None if not costs else
        str((sum(costs, Decimal(0)) / len(costs)).quantize(Decimal("0.01"))),
        "state": store.load_state(),
        "note": "Fill cost compares each fill with the close it was planned from; "
                "positive is worse. ADR-015 expects at most 10 bps over at least 10 fills.",
    }
