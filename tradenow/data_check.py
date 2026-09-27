"""Cross-check Tiingo imports against Alpaca before a registered run (ADR-013).

For every symbol, Tiingo's raw closes are compared with Alpaca's consolidated
(SIP) raw daily closes from CHECK_START on. A day is a mismatch if the closes
differ by more than CLOSE_TOLERANCE or the day exists in only one source. A
symbol passes if its mismatches are at most MAX_MISMATCH_FRACTION of the
shared days. The saved result names the universe's data hash, so a run can
require a passing check of exactly the data it evaluates.

check_symbol and summarize are pure; the functions below them read and write
one JSON file per universe hash.
"""

import json
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path

from .alpaca_paper import DailyBar
from .universe import Universe


CHECK_START = date(2016, 1, 4)
CLOSE_TOLERANCE = Decimal("0.005")
MAX_MISMATCH_FRACTION = Decimal("0.01")
MAX_LISTED = 10  # mismatching days shown per symbol


def check_symbol(tiingo: Mapping[date, Decimal], alpaca: list[DailyBar],
                 start: date, end: date) -> dict:
    """Compare one symbol's raw closes on the days from start to end."""
    ours = {day: close for day, close in tiingo.items() if start <= day <= end}
    theirs = {bar.date: bar.close for bar in alpaca if start <= bar.date <= end}
    shared = sorted(set(ours) & set(theirs))
    problems = [f"{day} only in Tiingo" for day in sorted(set(ours) - set(theirs))]
    problems += [f"{day} only in Alpaca" for day in sorted(set(theirs) - set(ours))]
    problems += [f"{day} close Tiingo {ours[day]} vs Alpaca {theirs[day]}"
                 for day in shared
                 if abs(theirs[day] - ours[day]) / ours[day] > CLOSE_TOLERANCE]
    passed = bool(shared) and len(problems) <= MAX_MISMATCH_FRACTION * len(shared)
    return {"shared_days": len(shared), "mismatches": len(problems), "passed": passed,
            "examples": sorted(problems)[:MAX_LISTED]}


def summarize(universe: Universe, results: Mapping[str, dict]) -> dict:
    missing = [symbol for symbol in universe.symbols if symbol not in results]
    if missing:
        raise ValueError(f"No check result for {', '.join(missing)}")
    return {"universe_sha256": universe.sha256, "symbols": list(universe.symbols),
            "start": CHECK_START.isoformat(), "end": universe.dates[-1].isoformat(),
            "close_tolerance": str(CLOSE_TOLERANCE),
            "max_mismatch_fraction": str(MAX_MISMATCH_FRACTION),
            "passed": all(results[symbol]["passed"] for symbol in universe.symbols),
            "failed_symbols": [symbol for symbol in universe.symbols
                               if not results[symbol]["passed"]],
            "results": {symbol: results[symbol] for symbol in universe.symbols}}


def check_path(directory: Path, universe_sha256: str) -> Path:
    return directory / f"data-check-{universe_sha256[:16]}.json"


def save_check(summary: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = check_path(directory, summary["universe_sha256"])
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return path


def require_passing_check(universe: Universe, directory: Path) -> dict:
    """The saved check of exactly this data, or an error saying what to do."""
    path = check_path(directory, universe.sha256)
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError("No data check for this data; run `data-check --broad` first") from None
    except (OSError, json.JSONDecodeError):
        raise ValueError(f"The data check file {path.name} cannot be read") from None
    if not isinstance(summary, dict) or summary.get("universe_sha256") != universe.sha256:
        raise ValueError("The data check does not match this data; run it again")
    if summary.get("passed") is not True:
        raise ValueError("The data check failed for " + ", ".join(
            summary.get("failed_symbols") or ["unknown symbols"])
            + "; investigate before the run (ADR-013)")
    return summary
