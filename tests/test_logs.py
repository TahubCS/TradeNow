import io
import json
import logging
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tradenow.__main__ import main
from tradenow.logs import (
    LOG_FILE,
    RUN_FILE,
    close_logging,
    configure_logging,
    read_runs,
    recorded_run,
    register_secret,
)
from tradenow.paper_trading import PaperBlocked


class RunLogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(close_logging)
        self.log_dir = Path(directory.name)

    def test_each_outcome_writes_exactly_one_record(self):
        with recorded_run("paper-status", self.log_dir, "paper") as run:
            run.note({"plan_id": "abc", "run_id": "report-1", "orders": [1, 2]})
            run.exit_code = 0
        with recorded_run("paper-plan", self.log_dir, "paper") as run:
            run.fail(PaperBlocked("MARKET_OPEN", "plan after the close"))
            run.exit_code = 1
        with self.assertRaises(RuntimeError):
            with recorded_run("paper-submit", self.log_dir, "paper"):
                raise RuntimeError("boom")
        with self.assertRaises(SystemExit):
            with recorded_run("gld", self.log_dir, "paper"):
                raise SystemExit(2)

        runs = read_runs(self.log_dir)
        self.assertEqual([item["outcome"] for item in runs], ["OK", "BLOCKED", "CRASH", "USAGE"])
        self.assertEqual(runs[0]["plan_id"], "abc")
        self.assertEqual(runs[0]["report_id"], "report-1")
        self.assertNotEqual(runs[0]["run_id"], "report-1")
        self.assertNotIn("orders", runs[0])
        self.assertEqual(runs[1]["code"], "MARKET_OPEN")
        self.assertEqual(runs[2]["error"], "RuntimeError: boom")
        self.assertEqual(len({item["run_id"] for item in runs}), 4)

    def test_registered_secrets_never_reach_log_files(self):
        secret = "sk-test-0123456789"
        register_secret(secret)
        logger = configure_logging(self.log_dir, console_level=logging.CRITICAL)
        logger.warning("request failed for %s", secret, extra={"fields": {"key": secret}})
        with recorded_run("tiingo-import", self.log_dir, "paper") as run:
            run.fail(ValueError(f"bad key {secret}"))
            run.exit_code = 1
        close_logging()
        for name in (LOG_FILE, RUN_FILE):
            text = (self.log_dir / name).read_text(encoding="utf-8")
            self.assertNotIn(secret, text)
            self.assertIn("[REDACTED]", text)

    def test_unwritable_run_log_does_not_hide_the_result(self):
        blocked = self.log_dir / "not-a-directory"
        blocked.write_text("", encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with recorded_run("paper-status", blocked, "paper") as run:
                run.exit_code = 0
        self.assertIn("run log not written", stderr.getvalue())

    def test_damaged_run_log_lines_are_skipped(self):
        (self.log_dir / RUN_FILE).write_text('{"outcome": "OK"}\nnot json\n', encoding="utf-8")
        self.assertEqual(read_runs(self.log_dir), [{"outcome": "OK"}])

    def test_cli_records_every_command_in_the_data_directory(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        missing = self.log_dir / "missing.csv"
        with patch.dict("os.environ", {"TRADENOW_DATA_DIR": str(self.log_dir)}):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(main(["gld", "--data", str(missing)]), 1)
                self.assertEqual(main(["stress", "--seeds", "1", "--days", "200",
                                       "--output", str(self.log_dir / "stress")]), 0)
        runs = read_runs(self.log_dir / "logs")
        self.assertEqual([(item["command"], item["outcome"]) for item in runs],
                         [("gld", "ERROR"), ("stress", "OK")])
        self.assertTrue(runs[1]["passed"])
        self.assertIn("gld ERROR", stderr.getvalue())
        self.assertTrue(json.loads(stdout.getvalue())["passed"])


if __name__ == "__main__":
    unittest.main()
