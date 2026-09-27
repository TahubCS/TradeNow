"""Command-line entry point. Every command is recorded once in the run log."""

import argparse
import hashlib
import json
import sys
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .alpaca_paper import PaperClient, load_paper_credentials
from .gld_research import MAX_GLD_CSV_BYTES, latest_imported_gld, run_gld_csv, save_gld_report
from .logs import Run, configure_logging, recorded_run
from .market_data import load_bars
from .notify import desktop_notify
from .offline import MAX_LOCAL_CSV_BYTES, run_local_csv, run_offline, save_offline
from .paper_auto import GLD_HISTORY_START, Notices, paper_auto
from .paper_trading import (
    PaperStore,
    paper_halt,
    paper_plan,
    paper_report,
    paper_resume,
    paper_status,
    paper_submit,
)
from .risk_config import load_risk
from .settings import load_settings
from .simulation import Config, simulate
from .stress import run_stress, save_stress
from .tiingo import import_complete, import_gld, load_api_key


def _fail(run: Run, error: BaseException) -> int:
    print(f"Error: {error}", file=sys.stderr)
    run.fail(error)
    return 1


def _emit(run: Run, result: dict) -> None:
    print(json.dumps(result, indent=2))
    run.note(result)


def offline_main(argv: list[str], run: Run) -> int:
    parser = argparse.ArgumentParser(description="Run offline research on synthetic or local CSV bars")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--days", type=int)
    parser.add_argument("--data", type=Path, help="Local MGC daily OHLCV CSV")
    parser.add_argument("--max-notional-ratio", default="0.50")
    parser.add_argument("--initial-margin-rate", default="0.10")
    parser.add_argument("--maintenance-margin-rate", default="0.08")
    parser.add_argument("--output", type=Path, default=Path("artifacts/offline"))
    args = parser.parse_args(argv)
    if args.data and (args.seed is not None or args.days is not None):
        parser.error("--data cannot be combined with --seed or --days")
    csv_text: str | bytes
    try:
        config = replace(Config(), max_notional_fraction=Decimal(args.max_notional_ratio),
                         initial_margin_fraction=Decimal(args.initial_margin_rate),
                         maintenance_margin_fraction=Decimal(args.maintenance_margin_rate))
        if args.data:
            if args.data.stat().st_size > MAX_LOCAL_CSV_BYTES:
                raise ValueError(f"Local CSV exceeds {MAX_LOCAL_CSV_BYTES} bytes")
            report, csv_text = run_local_csv(args.data.read_bytes(), args.data.name, config)
        else:
            report, csv_text = run_offline(args.seed if args.seed is not None else 3,
                                           args.days if args.days is not None else 360,
                                           config)
        bars_path, report_path, markdown_path = save_offline(report, csv_text, args.output)
    except (OSError, ValueError, InvalidOperation) as error:
        return _fail(run, error)

    _emit(run, {"mode": report["mode"], "run_id": report["run_id"],
                      "data_source": report["data"]["source"],
                      "contract": report["data"]["contract"],
                      "contracts": report["data"]["contracts"],
                      "source_sha256": report["data"]["sha256"],
                      "selected_hypothesis": report["research"]["selected_hypothesis"],
                      "holdout_summary": report["research"]["holdout_summary"],
                      "bars_file": str(bars_path.resolve()),
                      "report_file": str(report_path.resolve()),
                      "readable_report": str(markdown_path.resolve())})
    return 0


def stress_main(argv: list[str], run: Run) -> int:
    parser = argparse.ArgumentParser(description="Run synthetic scenario and fault checks")
    parser.add_argument("--seeds", type=int, default=12)
    parser.add_argument("--days", type=int, default=360)
    parser.add_argument("--output", type=Path, default=Path("artifacts/stress"))
    args = parser.parse_args(argv)
    try:
        report = run_stress(args.seeds, args.days)
        json_path, markdown_path = save_stress(report, args.output)
    except (OSError, ValueError) as error:
        return _fail(run, error)

    _emit(run, {"mode": report["mode"], "run_id": report["run_id"],
                      "passed": report["passed"],
                      "seed_runs": len(report["seed_runs"]),
                      "fault_cases": len(report["fault_cases"]),
                      "coverage": report["coverage"],
                      "report_file": str(json_path.resolve()),
                      "readable_report": str(markdown_path.resolve())})
    return 0 if report["passed"] else 1


def web_main(argv: list[str], run: Run) -> int:
    parser = argparse.ArgumentParser(description="Serve the local read-only simulation dashboard")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        import uvicorn
    except ModuleNotFoundError:
        return _fail(run, ValueError(
            'Uvicorn is required for the dashboard: python -m pip install -e ".[web]"'))
    uvicorn.run("tradenow.web:app", host="127.0.0.1", port=args.port)
    return 0


def tiingo_main(argv: list[str], run: Run) -> int:
    parser = argparse.ArgumentParser(description="Manually import private GLD daily data from Tiingo")
    parser.add_argument("--start", required=True, type=date.fromisoformat,
                        help="First requested date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, type=date.fromisoformat,
                        help="Last requested date (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    try:
        result = import_gld(args.start, args.end, load_api_key())
    except (OSError, ValueError) as error:
        return _fail(run, error)
    _emit(run, result)
    return 0


def gld_main(argv: list[str], run: Run) -> int:
    parser = argparse.ArgumentParser(description="Replay private GLD daily bars offline")
    parser.add_argument("--data", type=Path, help="GLD CSV; defaults to latest Tiingo import")
    parser.add_argument("--output", type=Path, default=Path("artifacts/gld"))
    args = parser.parse_args(argv)
    try:
        if args.data:
            if args.data.stat().st_size > MAX_GLD_CSV_BYTES:
                raise ValueError(f"GLD CSV exceeds {MAX_GLD_CSV_BYTES} bytes")
            source_bytes, filename = args.data.read_bytes(), args.data.name
            source = "local_gld_csv"
        else:
            source_bytes, filename = latest_imported_gld()
            source = "tiingo_eod_import"
        report = run_gld_csv(source_bytes, filename, source=source)
        json_path, md_path = save_gld_report(report, args.output)
    except (OSError, ValueError, InvalidOperation) as error:
        return _fail(run, error)
    _emit(run, {"mode": report["mode"], "run_id": report["run_id"],
                      "symbol": "GLD", "bars": report["data"]["bars"],
                      "source_sha256": report["data"]["sha256"],
                      "selected_hypothesis": report["research"]["selected_hypothesis"],
                      "holdout_summary": report["research"]["holdout_summary"],
                      "holdout_comparison": report["evaluation"]["holdout"],
                      "rolling_summary": report["evaluation"]["rolling_pre_holdout"]["summary"],
                      "live_gate": {"verdict": report["live_gate"]["verdict"],
                                    "failing_checks": report["live_gate"]["failing_checks"]},
                      "slippage_sensitivity": report["evaluation"]["slippage_sensitivity"],
                      "report_file": str(json_path.resolve()),
                      "readable_report": str(md_path.resolve())})
    return 0


def _gld_source(path: Path | None) -> tuple[bytes, str]:
    """A named GLD CSV, or the latest verified Tiingo import."""
    if path is None:
        return latest_imported_gld()
    if path.stat().st_size > MAX_GLD_CSV_BYTES:
        raise ValueError(f"GLD CSV exceeds {MAX_GLD_CSV_BYTES} bytes")
    return path.read_bytes(), path.name


def _import_through(end: date) -> dict:
    """paper-auto's import: skip a range that is already complete, else fetch or refresh."""
    if import_complete(GLD_HISTORY_START, end):
        return {"result": "ALREADY_COMPLETE"}
    return import_gld(GLD_HISTORY_START, end, load_api_key())


def _paper_auto_command(store: PaperStore, args: argparse.Namespace) -> dict:
    """Scheduled runs have no terminal, so a failure to even start is notified too."""
    try:
        risk = load_risk()
        client = PaperClient(load_paper_credentials())
    except (OSError, ValueError) as error:
        Notices(store, desktop_notify, date.today()).send(
            f"setup:{type(error).__name__}", "Paper-auto cannot start", str(error))
        raise
    return paper_auto(client, store, risk, _import_through, lambda: _gld_source(None),
                      desktop_notify, load_settings().log_dir, args.dry_run, args.check)


def paper_main(command: str, argv: list[str], run: Run) -> int:
    """Alpaca paper commands; only these contact the paper trading API
    (paper-report reads local files only)."""
    parser = argparse.ArgumentParser(prog=f"tradenow {command}")
    if command in ("paper-plan", "paper-report"):
        parser.add_argument("--data", type=Path, help="GLD CSV; defaults to latest Tiingo import")
    elif command == "paper-submit":
        parser.add_argument("--approve", required=True, metavar="PLAN_ID",
                            help="Approve and send the order in this saved plan")
    elif command == "paper-halt":
        parser.add_argument("--reason", required=True)
        parser.add_argument("--flatten", action="store_true",
                            help="Also sell the whole GLD position at market")
    elif command == "paper-resume":
        parser.add_argument("--confirm", action="store_true", required=True)
    elif command == "paper-auto":
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true",
                          help="Plan only, even if risk.toml sets auto_submit = true")
        mode.add_argument("--check", action="store_true",
                          help="Morning check: reconcile and report fills; never trades")
    args = parser.parse_args(argv)
    store = PaperStore()
    try:
        if command == "paper-auto":
            result = _paper_auto_command(store, args)
            _emit(run, result)
            return 0
        risk = load_risk()
        if command == "paper-report":
            _emit(run, paper_report(store, *_gld_source(args.data), risk=risk,
                                    include_gate=True))
            return 0
        client = PaperClient(load_paper_credentials())
        if command == "paper-status":
            result = paper_status(client, store, risk)
        elif command == "paper-plan":
            result = paper_plan(client, store, *_gld_source(args.data), risk)
        elif command == "paper-submit":
            result = paper_submit(client, store, args.approve)
        elif command == "paper-halt":
            result = paper_halt(client, store, args.reason, args.flatten)
        else:
            result = paper_resume(client, store)
    except (OSError, ValueError, InvalidOperation) as error:
        return _fail(run, error)
    _emit(run, result)
    return 1 if "error" in result else 0


PAPER_COMMANDS = ("paper-status", "paper-plan", "paper-submit", "paper-halt", "paper-resume",
                  "paper-report", "paper-auto")


def replay_main(argv: list[str], run: Run) -> int:
    parser = argparse.ArgumentParser(
        description="Replay local MGC bars without network or broker access",
        epilog=("Use 'gld', 'offline', 'stress', 'web', or 'tiingo-import' for the research "
                "tools, and 'paper-status', 'paper-plan', 'paper-submit', 'paper-halt', "
                "'paper-resume', 'paper-report', or 'paper-auto' for Alpaca paper trading."),
    )
    parser.add_argument("--data", type=Path,
                        default=Path(__file__).resolve().parent.parent / "sample_data" / "mgc_synthetic.csv",
                        help="Path to a local CSV with one futures contract")
    args = parser.parse_args(argv)

    try:
        source = args.data.read_bytes()
        bars = load_bars(args.data)
        result = simulate(bars)
    except (OSError, ValueError) as error:
        return _fail(run, error)

    result["source_sha256"] = hashlib.sha256(source).hexdigest()
    result["config"] = {key: str(value) for key, value in vars(Config()).items()}
    _emit(run, result)
    return 0


def notify_test_main(argv: list[str], run: Run) -> int:
    """Show one test notification and report exactly what Windows did."""
    argparse.ArgumentParser(prog="tradenow notify-test",
                            description="Send a test desktop notification").parse_args(argv)
    result = desktop_notify("TradeNow test", "If you can read this, notifications work.")
    _emit(run, {"result": "SHOWN" if result["shown"] else "NOT_SHOWN", **result})
    return 0 if result["shown"] else 1


COMMANDS = {"offline": offline_main, "stress": stress_main, "web": web_main,
            "tiingo-import": tiingo_main, "gld": gld_main, "notify-test": notify_test_main}


def main(argv: list[str] | None = None) -> int:
    """Dispatch one command and record it in the run log, whatever the outcome."""
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv and (argv[0] in COMMANDS or argv[0] in PAPER_COMMANDS) else ""
    rest = argv[1:] if command else argv
    settings = load_settings()
    configure_logging(settings.log_dir)
    with recorded_run(command or "replay", settings.log_dir, settings.mode) as run:
        if command in PAPER_COMMANDS:
            run.exit_code = paper_main(command, rest, run)
        elif command:
            run.exit_code = COMMANDS[command](rest, run)
        else:
            run.exit_code = replay_main(rest, run)
    return run.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
