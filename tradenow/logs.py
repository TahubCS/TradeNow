"""Logging and the per-command run log.

Two files under the private log directory, both JSON lines:

- tradenow.jsonl: every log record from the package (rotated at 5 MB).
- runs.jsonl: exactly one record per CLI command, with its outcome.

The terminal gets a short readable line per record on stderr, so stdout keeps
carrying only each command's JSON result. Credential values registered with
register_secret are replaced with [REDACTED] in everything written.
"""

import json
import logging
import logging.handlers
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


LOGGER_NAME = "tradenow"
LOG_FILE = "tradenow.jsonl"
RUN_FILE = "runs.jsonl"
MAX_LOG_BYTES = 5_000_000
LOG_BACKUPS = 5
# Result keys worth keeping in the run log; everything else stays in the command's output.
SUMMARY_KEYS = ("plan_id", "action", "reason", "result", "passed", "kill_switch",
                "bars", "last_bar", "bars_sha256", "previous_last_bar")
_SECRETS: set[str] = set()
_HANDLERS: list[logging.Handler] = []


def register_secret(value: str) -> None:
    """Remember a credential so no log line can contain it. Very short values are
    ignored because replacing them would mangle ordinary text."""
    if len(value) >= 8:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class JsonFormatter(logging.Formatter):
    """One JSON object per line; structured values go in extra={"fields": {...}}."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds"),
            "level": record.levelname, "logger": record.name,
            "message": record.getMessage()}
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update(fields)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return redact(json.dumps(entry, default=str))


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def close_logging() -> None:
    """Remove the handlers configure_logging installed and close their files."""
    logger = logging.getLogger(LOGGER_NAME)
    while _HANDLERS:
        handler = _HANDLERS.pop()
        logger.removeHandler(handler)
        handler.close()


def configure_logging(log_dir: Path, console_level: int = logging.INFO) -> logging.Logger:
    """Install the file and terminal handlers once; calling again replaces them."""
    close_logging()
    logger = logging.getLogger(LOGGER_NAME)
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / LOG_FILE, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    file_handler.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(_RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
    console.setLevel(console_level)
    for handler in (file_handler, console):
        _HANDLERS.append(handler)
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger


def summarize(result: object) -> dict:
    """The few fields of a command's result that identify what it did."""
    if not isinstance(result, dict):
        return {}
    summary = {key: result[key] for key in SUMMARY_KEYS
               if key in result and isinstance(result[key], (str, int, bool, type(None)))}
    if isinstance(result.get("run_id"), str):
        # A research report's own ID; the run log keeps its own run_id.
        summary["report_id"] = result["run_id"]
    for key in ("order", "flatten"):
        nested = result.get(key)
        if isinstance(nested, dict):
            order = nested.get("order", nested)
            if isinstance(order, dict) and "client_order_id" in order:
                summary["client_order_id"] = order["client_order_id"]
                summary["order_status"] = order.get("status")
    if isinstance(result.get("error"), str):
        summary["error"] = result["error"]
    return summary


class Run:
    """What one command did. The CLI fills this in; recorded_run writes it."""

    def __init__(self, command: str, mode: str):
        self.record: dict[str, Any] = {"run_id": uuid4().hex[:12], "command": command,
                                       "mode": mode, "started_at": _now()}
        self.exit_code: int | None = None

    def note(self, result: object) -> None:
        self.record.update(summarize(result))

    def fail(self, error: BaseException) -> None:
        self.record["error"] = str(error)
        code = getattr(error, "code", None)
        if isinstance(code, str):
            self.record["code"] = code


def _outcome(run: Run) -> str:
    if run.exit_code == 0:
        return "OK"
    return "BLOCKED" if "code" in run.record else "ERROR"


@contextmanager
def recorded_run(command: str, log_dir: Path, mode: str) -> Iterator[Run]:
    """Append exactly one record to runs.jsonl, however the command ends."""
    run = Run(command, mode)
    started = time.monotonic()
    try:
        yield run
    except SystemExit as stop:
        # argparse exits for --help (0) and for invalid arguments (2).
        run.exit_code = stop.code if isinstance(stop.code, int) else 1
        run.record["outcome"] = "OK" if run.exit_code == 0 else "USAGE"
        raise
    except BaseException as error:
        run.exit_code = 1
        run.record.update(outcome="CRASH", error=f"{type(error).__name__}: {error}")
        raise
    else:
        run.record["outcome"] = _outcome(run)
    finally:
        run.record.update(finished_at=_now(), exit_code=run.exit_code,
                          duration_s=round(time.monotonic() - started, 3))
        logger = logging.getLogger(LOGGER_NAME)
        try:
            _append(log_dir / RUN_FILE, run.record)
        except OSError as error:
            # The command has already acted; a full disk must not hide its result.
            print(f"Warning: run log not written: {error}", file=sys.stderr)
        level = logging.INFO if run.record["outcome"] == "OK" else logging.WARNING
        detail = run.record.get("code") or run.record.get("error") or ""
        logger.log(level, "%s %s in %.1fs%s (run %s)", command, run.record["outcome"],
                   run.record["duration_s"], f": {detail}" if detail else "",
                   run.record["run_id"], extra={"fields": {"run": run.record}})


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(redact(json.dumps(record, default=str)) + "\n")


def read_runs(log_dir: Path) -> list[dict]:
    """Every readable run record, oldest first; a damaged line is skipped, not fatal."""
    path = log_dir / RUN_FILE
    if not path.exists():
        return []
    runs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            runs.append(item)
    return runs
