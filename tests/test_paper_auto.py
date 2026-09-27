import os
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tests.test_paper_trading import LAST, SOURCE, FakeBroker
from tradenow.alpaca_paper import DailyBar
from tradenow.notify import SECRET_PREFIXES, TOAST_SCRIPT, desktop_notify
from tradenow.paper_auto import LOCK_NAME, paper_auto
from tradenow.paper_trading import PaperBlocked, PaperStore, paper_report
from tradenow.risk_config import RiskConfig, default_risk, risk_sha256


def risk(auto_submit: bool):
    config = RiskConfig(auto_submit=auto_submit)
    return replace(default_risk(), config=config, sha256=risk_sha256(config))


class PaperAutoTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = PaperStore(self.root / "alpaca")
        self.broker = FakeBroker()
        self.notices: list[tuple[str, str]] = []
        self.imports: list[date] = []

    def run_auto(self, auto_submit: bool = True, **options) -> dict:
        def import_through(end):
            self.imports.append(end)
            return {"result": "IMPORTED"}

        return paper_auto(self.broker, self.store, risk(auto_submit), import_through,
                          lambda: (SOURCE, "GLD.csv"),
                          lambda title, message: self.notices.append((title, message)),
                          self.root / "logs", **options)

    def test_auto_submit_sends_once_and_later_runs_wait(self):
        result = self.run_auto()
        self.assertEqual((result["action"], result["submission"]), ("BUY", "SUBMITTED"))
        self.assertEqual(result["mode"], "auto_submit")
        self.assertEqual(self.imports, [LAST.date])
        self.assertEqual(self.store.load_ledger().orders[0].approval, "auto")
        self.assertEqual([title for title, _ in self.notices], ["Paper order sent"])

        again = self.run_auto()
        self.assertEqual(again["result"], "WAIT")
        self.assertEqual(self.broker.submissions, 1)
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.store.auto_status()["result"], "WAIT")

    def test_dry_run_never_submits_and_notifies_once_per_day(self):
        for options in ({"auto_submit": False}, {"auto_submit": True, "dry_run": True}):
            with self.subTest(**options):
                result = self.run_auto(**options)
                self.assertEqual(result["submission"], "NOT_SENT_DRY_RUN")
                self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(self.broker.submissions, 0)
        self.assertEqual([title for title, _ in self.notices], ["Paper-auto dry run"])
        self.assertIn("auto_submit is off", self.notices[0][1])

    def test_waits_quietly_until_tiingo_has_the_close(self):
        self.broker.bars.append(DailyBar(date(2025, 12, 9), LAST.close, LAST.close, 1))
        result = self.run_auto()
        self.assertEqual(result["result"], "WAITING_FOR_DATA")
        self.assertEqual((self.broker.submissions, self.notices), (0, []))

    def test_kill_switch_blocks_and_notifies_once_per_day(self):
        self.store.engage_kill_switch("manual test")
        for _ in range(2):
            with self.assertRaisesRegex(PaperBlocked, "KILL_SWITCH_ENGAGED"):
                self.run_auto()
        self.assertEqual(self.imports, [])
        self.assertEqual([title for title, _ in self.notices], ["Paper-auto blocked"])

    def test_only_one_run_at_a_time_but_a_stale_lock_is_taken_over(self):
        lock = self.store.directory / LOCK_NAME
        lock.parent.mkdir(parents=True)
        lock.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(PaperBlocked, "ALREADY_RUNNING"):
            self.run_auto()
        old = lock.stat().st_mtime - 3 * 3600
        os.utime(lock, (old, old))
        self.assertEqual(self.run_auto()["action"], "BUY")
        self.assertFalse(lock.exists())

    def test_morning_check_reports_fills_and_never_trades(self):
        sent = self.run_auto()
        client_order_id = self.store.load_ledger().orders[0].client_order_id
        self.broker.fill(client_order_id, Decimal("300.25"))
        checked = self.run_auto(check_only=True)
        self.assertEqual(checked["finished_orders"], [client_order_id])
        self.assertEqual(self.notices[-1][0], "Paper order finished")
        self.assertIn("filled at $300.25", self.notices[-1][1])
        self.assertEqual(self.broker.submissions, 1)
        self.assertEqual(self.run_auto(check_only=True)["finished_orders"], [])
        self.assertEqual(sent["submission"], "SUBMITTED")

    def test_gate_change_is_notified_but_first_fail_is_not(self):
        self.run_auto(auto_submit=False)
        self.assertEqual(self.store.saved_gate()["verdict"], "FAIL")
        self.assertNotIn("Live-trading gate", " ".join(title for title, _ in self.notices))

        def passing(*args, **kwargs):
            report = paper_report(*args, **kwargs)
            report["live_gate"] = {**report["live_gate"], "verdict": "PASS",
                                   "failing_checks": []}
            return report

        self.broker.next_open += timedelta(days=1)
        with patch("tradenow.paper_auto.paper_report", side_effect=passing):
            result = self.run_auto(auto_submit=False)
        self.assertEqual(result["live_gate"]["verdict"], "PASS")
        self.assertEqual(self.notices[-1][0], "Live-trading gate PASSED")
        self.assertIn("nothing was enabled", self.notices[-1][1])


    def test_gate_failure_does_not_hide_a_sent_order(self):
        with patch("tradenow.paper_auto.paper_report", side_effect=ValueError("bad data")):
            result = self.run_auto()
        self.assertEqual(result["submission"], "SUBMITTED")
        self.assertEqual(result["live_gate"], {"error": "bad data"})


class NotifyTests(unittest.TestCase):
    def test_off_windows_it_only_logs(self):
        with patch("tradenow.notify.IS_WINDOWS", False), \
                patch("tradenow.notify.subprocess.run") as run:
            desktop_notify("Title", "Message")
        run.assert_not_called()

    def test_text_travels_in_environment_never_in_the_command(self):
        hostile = "'; Remove-Item C:\\ -Recurse; '<x/>\n"
        environment = {"SystemRoot": "C:\\Windows", "TIINGO_API_KEY": "k",
                       "ALPACA_PAPER_SECRET_KEY": "s", "PATH": "C:\\bin"}
        with patch("tradenow.notify.IS_WINDOWS", True), \
                patch.dict("tradenow.notify.os.environ", environment, clear=True), \
                patch("tradenow.notify.powershell_path", return_value=Path("C:/ps.exe")), \
                patch("tradenow.notify.subprocess.run") as run:
            desktop_notify("Paper order sent", hostile)
        command = run.call_args.args[0]
        self.assertEqual(command[-1], TOAST_SCRIPT)
        self.assertNotIn("Remove-Item", " ".join(command))
        child = run.call_args.kwargs["env"]
        self.assertIn("Remove-Item", child["TRADENOW_TOAST_MESSAGE"])
        self.assertNotIn("\n", child["TRADENOW_TOAST_MESSAGE"])
        self.assertFalse([name for name in child if name.upper().startswith(SECRET_PREFIXES)])
        self.assertEqual(child["PATH"], "C:\\bin")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_notification_failure_does_not_raise(self):
        with patch("tradenow.notify.IS_WINDOWS", True), \
                patch("tradenow.notify.powershell_path", return_value=Path("C:/ps.exe")), \
                patch("tradenow.notify.subprocess.run", side_effect=OSError("no desktop")):
            desktop_notify("Title", "Message")  # logged as a warning, not raised


if __name__ == "__main__":
    unittest.main()
