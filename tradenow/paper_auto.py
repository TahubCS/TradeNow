"""paper-auto: the scheduled GLD paper routine without per-order approval (ADR-009).

Every step reuses the manual commands' code: kill switch, reconciliation, the
Tiingo/Alpaca data cross-check, the risk checks, the saved plan, and the
at-most-once submission. risk.toml's auto_submit decides whether an order is
sent (true) or only planned (false: a dry run). Only the Alpaca paper account
can be reached; there is no live mode.

Evening run: import the close, plan, submit, measure, and check the
live-trading gate. Morning run (check_only): reconcile and report fills; it
never trades.
"""

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .notify import Notifier
from .paper_trading import (
    BrokerClient,
    PaperBlocked,
    PaperStore,
    _reconciled,
    paper_plan,
    paper_report,
    paper_submit,
)
from .risk_config import LoadedRisk


GLD_HISTORY_START = date(2004, 11, 18)
LOCK_NAME = "paper-auto.lock"
# A lock older than this was left by a crashed run and may be taken over.
STALE_LOCK_AGE = timedelta(hours=2)
WAITING_CODES = ("TIINGO_STALE",)


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    """At most one paper-auto at a time; the ledger has a single writer (ADR-007)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        handle = os.open(path, flags)
    except FileExistsError:
        try:
            age = datetime.now(timezone.utc) - datetime.fromtimestamp(path.stat().st_mtime,
                                                                       timezone.utc)
        except FileNotFoundError:
            age = STALE_LOCK_AGE
        if age < STALE_LOCK_AGE:
            raise PaperBlocked("ALREADY_RUNNING", "another paper-auto run holds the lock") from None
        path.unlink(missing_ok=True)
        try:
            handle = os.open(path, flags)
        except FileExistsError:
            raise PaperBlocked("ALREADY_RUNNING", "another paper-auto run took the lock") from None
    with os.fdopen(handle, "w", encoding="utf-8") as lock:
        lock.write(json.dumps({"pid": os.getpid(),
                               "started_at": datetime.now(timezone.utc).isoformat()}))
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


class Notices:
    """Send each distinct notice at most once per day, so a blocked evening
    of scheduled retries produces one notification, not nine."""

    def __init__(self, store: PaperStore, notify: Notifier, day: date):
        self.path = store.directory / "notices.json"
        self.notify, self.day = notify, day.isoformat()

    def send(self, key: str, title: str, message: str, once_per_day: bool = True) -> None:
        try:
            sent = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            sent = {}
        if not isinstance(sent, dict):
            sent = {}
        if once_per_day and sent.get(key) == self.day:
            return
        self.notify(title, message)
        sent = {name: day for name, day in sent.items() if day == self.day}
        sent[key] = self.day
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sent), encoding="utf-8")


def _describe(order: dict) -> str:
    price = f" limit ${order['limit_price']}" if order.get("limit_price") else " at market"
    return f"{order['side'].upper()} {order['qty']} GLD{price}"


def _morning_check(client: BrokerClient, store: PaperStore, notices: Notices) -> dict:
    """Reconcile and report orders that finished since the evening run."""
    before = {order.client_order_id: order for order in store.load_ledger().orders}
    ledger, reconciliation = _reconciled(client, store, engage_on_mismatch=True)
    finished = []
    for order in ledger.orders:
        earlier = before.get(order.client_order_id)
        if earlier is None or earlier.is_final or not order.is_final:
            continue
        finished.append(order.client_order_id)
        if order.filled_qty:
            message = (f"{order.side.upper()} {order.filled_qty} GLD filled at "
                       f"${order.filled_avg_price}")
        else:
            message = f"{order.side.upper()} {order.qty} GLD ended {order.status}, unfilled"
        notices.send(f"order:{order.client_order_id}", "Paper order finished", message,
                     once_per_day=False)
    return {"result": "CHECKED", "finished_orders": finished,
            "gld_shares": reconciliation.broker_shares}


def _check_gate(store: PaperStore, source: tuple[bytes, str], risk: LoadedRisk,
                log_dir: Path, notices: Notices) -> dict:
    previous = store.saved_gate()
    report = paper_report(store, *source, log_dir, risk, include_gate=True)
    gate = report["live_gate"]
    earlier = None if previous is None else previous.get("verdict")
    if gate["verdict"] != earlier and (earlier is not None or gate["verdict"] == "PASS"):
        title = ("Live-trading gate PASSED" if gate["verdict"] == "PASS"
                 else "Live-trading gate no longer passes")
        message = ("A strategy now beats buy-and-hold under ADR-008. Review before any real "
                   "money; nothing was enabled." if gate["verdict"] == "PASS" else
                   "Failing: " + ", ".join(gate["failing_checks"]))
        notices.send(f"gate:{gate['verdict']}", title, message, once_per_day=False)
    return {"verdict": gate["verdict"], "failing_checks": gate["failing_checks"],
            "execution": {key: report["summary"][key]
                          for key in ("orders", "fill_rate_pct", "mean_slippage_bps")}}


def paper_auto(client: BrokerClient, store: PaperStore, risk: LoadedRisk,
               import_through: Callable[[date], dict],
               load_source: Callable[[], tuple[bytes, str]], notify: Notifier,
               log_dir: Path, dry_run: bool = False, check_only: bool = False) -> dict:
    """One scheduled run. Refusals raise PaperBlocked, like the manual commands."""
    notices = Notices(store, notify, date.today())
    with run_lock(store.directory / LOCK_NAME):
        try:
            kill = store.kill_switch()
            if kill is not None:
                raise PaperBlocked("KILL_SWITCH_ENGAGED",
                                   f"{kill.get('reason')}; run paper-resume after review")
            if check_only:
                result = _morning_check(client, store, notices)
            else:
                result = _evening(client, store, risk, import_through, load_source,
                                  log_dir, dry_run, notices)
        except PaperBlocked as error:
            if error.code not in WAITING_CODES and not any(
                    code in str(error) for code in WAITING_CODES):
                notices.send(f"blocked:{error.code}", "Paper-auto blocked", str(error))
            raise
        except (OSError, ValueError) as error:
            notices.send(f"error:{type(error).__name__}", "Paper-auto error", str(error))
            raise
    result["mode"] = "dry_run" if dry_run or not risk.config.auto_submit else "auto_submit"
    store.save_auto_status({**result, "finished_at": datetime.now(timezone.utc).isoformat()})
    return result


def _evening(client: BrokerClient, store: PaperStore, risk: LoadedRisk,
             import_through: Callable[[date], dict],
             load_source: Callable[[], tuple[bytes, str]], log_dir: Path,
             dry_run: bool, notices: Notices) -> dict:
    # New York's date from Alpaca's clock; never past the local date the importer allows.
    end = min(client.clock().timestamp.date(), date.today())
    imported = import_through(end)
    source = load_source()
    try:
        plan = paper_plan(client, store, *source, risk)
    except PaperBlocked as error:
        if "TIINGO_STALE" in str(error):
            return {"result": "WAITING_FOR_DATA", "import": imported.get("result"),
                    "detail": str(error)}
        raise
    result = {"result": plan.get("action"), "import": imported.get("result"),
              "plan_id": plan.get("plan_id"), "action": plan.get("action"),
              "reason": plan.get("reason")}
    order = plan.get("order")
    if order:
        if risk.config.auto_submit and not dry_run:
            submitted = paper_submit(client, store, plan["plan_id"], approval="auto")
            result["submission"] = submitted["result"]
            if submitted["result"] != "ALREADY_SUBMITTED":
                notices.send(f"sent:{order['client_order_id']}", "Paper order sent",
                             f"{_describe(order)} ({plan['reason']})", once_per_day=False)
        else:
            result["submission"] = "NOT_SENT_DRY_RUN"
            notices.send(f"dry:{plan['session_open']}", "Paper-auto dry run",
                         f"Would send {_describe(order)} ({plan['reason']}); "
                         "auto_submit is off")
    try:
        result["live_gate"] = _check_gate(store, source, risk, log_dir, notices)
    except (OSError, ValueError) as error:
        # Measuring must never hide what the run already did (such as sending an order).
        result["live_gate"] = {"error": str(error)}
    return result
