"""The experiment log: one record per distinct research run (evaluation.md).

Each `gld` run appends a record to experiments.jsonl in the private data
directory. The experiment ID is the report's run ID, a hash of the data, code,
candidates, and configuration, so re-running the same experiment adds nothing,
and every distinct trial is counted exactly once. That count matters: the more
candidates are tried, the likelier one looks good by chance (ADR-008).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from .settings import PROJECT_ROOT, SETTINGS


EXPERIMENT_FILE = SETTINGS.data_dir / "experiments.jsonl"
# Until Phase 2 adds versioned features, the only feature is the SMA inside the simulator.
FEATURE_VERSION = "inline_sma_v0"
STRATEGY_FAMILY = "gld_sma_candidates_v1"


def code_commit(root: Path = PROJECT_ROOT) -> str | None:
    """The checked-out Git commit, read from .git without running git."""
    git = root / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head or None
        ref = head[5:]
        loose = git / ref
        if loose.exists():
            return loose.read_text(encoding="utf-8").strip() or None
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0]
    except (OSError, UnicodeDecodeError):
        return None
    return None


def experiment_record(report: dict) -> dict:
    evaluation, research, data = report["evaluation"], report["research"], report["data"]
    holdout = evaluation["holdout"]
    return {
        "experiment_id": report["run_id"],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "strategy_version": STRATEGY_FAMILY,
        "candidates": [item["name"] for item in research["hypotheses"]],
        "selected_hypothesis": research["selected_hypothesis"],
        "feature_version": FEATURE_VERSION,
        "config": report["config"],
        "data": {key: data[key] for key in ("source_name", "sha256", "first_date",
                                            "last_date", "bars")},
        "code_sha256": report["code_sha256"], "code_commit": code_commit(),
        "results": {
            "holdout_strategy": holdout["strategy"]["metrics"],
            "holdout_buy_hold_100pct": holdout["buy_hold_100pct"]["metrics"],
            "rolling": evaluation["rolling_pre_holdout"]["summary"],
            "rolling_stressed": evaluation["rolling_pre_holdout_stressed"]["summary"],
            "live_gate": {"verdict": report["live_gate"]["verdict"],
                          "failing_checks": report["live_gate"]["failing_checks"]}},
        "notes": "",
    }


def read_experiments(path: Path = EXPERIMENT_FILE) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def record_experiment(report: dict, path: Path = EXPERIMENT_FILE) -> dict:
    """Append the run unless this exact experiment is already logged."""
    existing = read_experiments(path)
    known = {item.get("experiment_id") for item in existing}
    new = report["run_id"] not in known
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(experiment_record(report)) + "\n")
    return {"experiment_id": report["run_id"], "recorded": new,
            "distinct_experiments": len(known | {report["run_id"]}),
            "file": str(path)}
