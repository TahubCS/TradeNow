"""Command-line entry point. Every command is recorded once in the run log."""

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .alpaca_paper import PaperClient, load_paper_credentials
from .data_check import CHECK_START, check_symbol, require_passing_check, save_check, summarize
from .equity import EquityConfig
from .experiments import multi_experiment_record, record_experiment
from .features import snapshot
from .gld_research import (
    MAX_GLD_CSV_BYTES,
    latest_imported_gld,
    parse_gld_csv,
    run_gld_csv,
    save_gld_report,
)
from .logs import Run, configure_logging, recorded_run
from .market_data import load_bars
from .multi_research import MULTI_STUDY, Study, run_study, save_multi_report
from .notify import desktop_notify
from .offline import MAX_LOCAL_CSV_BYTES, run_local_csv, run_offline, save_offline
from .paper_auto import GLD_HISTORY_START, Notices, paper_auto
from .paper_trading import (
    PaperStore,
    _jsonable,
    paper_halt,
    paper_plan,
    paper_report,
    paper_resume,
    paper_status,
    paper_submit,
)
from .risk_config import load_risk
from .selection import RESEARCH_CONFIG
from .settings import load_settings
from .simulation import Config, simulate
from .stress import run_stress, save_stress
from .tiingo import import_complete, import_gld, import_symbol, load_api_key
from .universe import BROAD_UNIVERSE, UNIVERSE, describe, load_universe


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
    parser = argparse.ArgumentParser(description="Manually import private daily data from Tiingo")
    parser.add_argument("--start", required=True, type=date.fromisoformat,
                        help="First requested date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, type=date.fromisoformat,
                        help="Last requested date (YYYY-MM-DD)")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--symbols", help="Comma-separated tickers (default: GLD)")
    which.add_argument("--universe", action="store_true",
                       help=f"The ADR-011 symbols: {', '.join(UNIVERSE)}")
    which.add_argument("--broad", action="store_true",
                       help=f"The {len(BROAD_UNIVERSE)} ADR-013 symbols; already complete "
                            "imports are skipped, so a stopped run resumes")
    args = parser.parse_args(argv)
    symbols = (list(UNIVERSE) if args.universe else list(BROAD_UNIVERSE) if args.broad else
               [item.strip().upper() for item in args.symbols.split(",")] if args.symbols
               else ["GLD"])
    if len(symbols) == 1:
        try:
            result = import_symbol(symbols[0], args.start, args.end, load_api_key())
        except (OSError, ValueError) as error:
            return _fail(run, error)
        _emit(run, result)
        return 0
    # Several symbols: stop at the first failure (a rate limit must not be retried)
    # and still report what was imported before it.
    results: dict[str, object] = {}
    try:
        key = load_api_key()
        for symbol in symbols:
            if import_complete(args.start, args.end, symbol=symbol):
                results[symbol] = {"result": "ALREADY_COMPLETE"}
                continue
            results[symbol] = {name: value for name, value in
                               import_symbol(symbol, args.start, args.end, key).items()
                               if name in ("result", "bars", "first_bar", "last_bar",
                                           "pruned_files")}
    except (OSError, ValueError) as error:
        _emit(run, {"result": "STOPPED", "error": str(error), "symbols": results})
        run.fail(error)
        return 1
    summary: dict[str, object] = {"result": "IMPORTED", "symbols": results}
    if args.universe or args.broad:
        try:
            summary["universe"] = describe(load_universe(
                symbols=BROAD_UNIVERSE if args.broad else UNIVERSE))
        except (OSError, ValueError) as error:
            summary["universe"] = {"error": str(error)}
    _emit(run, _jsonable(summary))
    return 0


def universe_main(argv: list[str], run: Run) -> int:
    """Check that the latest imports of the registered symbols align, offline."""
    argparse.ArgumentParser(prog="tradenow universe",
                            description="Summarize the aligned multi-asset history").parse_args(argv)
    try:
        result = describe(load_universe())
    except (OSError, ValueError) as error:
        return _fail(run, error)
    _emit(run, _jsonable(result))
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
        # Registered research setting (ADR-010): full position cap, all candidates.
        report = run_gld_csv(source_bytes, filename, RESEARCH_CONFIG, source)
        json_path, md_path = save_gld_report(report, args.output)
        experiment = record_experiment(report)
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
                      "holdout_metrics": report["evaluation"]["holdout"]["strategy"]["metrics"],
                      "experiment": experiment,
                      "slippage_sensitivity": report["evaluation"]["slippage_sensitivity"],
                      "report_file": str(json_path.resolve()),
                      "readable_report": str(md_path.resolve())})
    return 0


def _study_main(argv: list[str], run: Run, command: str, description: str,
                load_study: Callable[[], Study], require_check: bool = False) -> int:
    """A registered multi-asset study on the latest imports: reports, one
    experiment record per distinct run, and the gate verdict."""
    parser = argparse.ArgumentParser(prog=f"tradenow {command}", description=description)
    parser.add_argument("--output", type=Path, default=Path("artifacts") / command)
    args = parser.parse_args(argv)
    settings = load_settings()
    try:
        study = load_study()
        universe = load_universe(settings.tiingo_dir, study.symbols)
        check = (require_passing_check(universe, settings.data_dir / "checks")
                 if require_check else None)
        report = run_study(universe, study)
        json_path, md_path = save_multi_report(report, args.output)
        experiment = record_experiment(report, settings.data_dir / "experiments.jsonl",
                                       multi_experiment_record)
    except (OSError, ValueError, InvalidOperation) as error:
        return _fail(run, error)
    evaluation = report["evaluation"]
    _emit(run, {"mode": report["mode"], "run_id": report["run_id"], "adr": report["adr"],
                "symbols": report["data"]["symbols"], "bars": report["data"]["bars"],
                "data_sha256": report["data"]["sha256"],
                "candidates_evaluated": len(report["candidates"]),
                "registered_trials_total": report["registered_trials_total"],
                "rolling_summary": evaluation["rolling_pre_holdout"]["summary"],
                "rolling_stressed_summary":
                    evaluation["rolling_pre_holdout_stressed"]["summary"],
                "holdout_selected": evaluation["selection"]["selected_hypothesis"],
                "live_gate": {"verdict": report["live_gate"]["verdict"],
                              "research_passed": report["live_gate"]["research"]["passed"],
                              "failing_checks": report["live_gate"]["failing_checks"]},
                "data_check": None if check is None else {
                    "passed": check["passed"], "universe_sha256": check["universe_sha256"]},
                "experiment": experiment,
                "report_file": str(json_path.resolve()),
                "readable_report": str(md_path.resolve())})
    return 0


def multi_main(argv: list[str], run: Run) -> int:
    """The registered rule candidates (ADR-011)."""
    return _study_main(argv, run, "multi", "Evaluate the six registered multi-asset rule "
                       "candidates against B1 and B2", lambda: MULTI_STUDY)


def _ml_study() -> Study:
    # Loaded only here, so every other command runs without scikit-learn.
    try:
        from .ml_research import ML_STUDY
    except ImportError as error:
        raise ValueError(f"The ml command needs the pinned model libraries ({error.name} is "
                         "missing). Install them with: python -m pip install -e \".[ml]\", "
                         "or run it with the project's .venv Python") from None
    return ML_STUDY


def _broad_study() -> Study:
    try:
        from .broad_research import BROAD_STUDY
    except ImportError as error:
        raise ValueError(f"The broad command needs the pinned model libraries ({error.name} "
                         "is missing). Install them with: python -m pip install -e \".[ml]\", "
                         "or run it with the project's .venv Python") from None
    return BROAD_STUDY


def broad_main(argv: list[str], run: Run) -> int:
    """The registered broad ETF universe study (ADR-013); needs a passing data check."""
    return _study_main(argv, run, "broad", "Evaluate the ten registered candidates on the "
                       "35-ETF universe against B1 and B2", _broad_study, require_check=True)


def data_check_main(argv: list[str], run: Run) -> int:
    """Compare the latest Tiingo imports with Alpaca's daily closes (ADR-013)."""
    parser = argparse.ArgumentParser(prog="tradenow data-check",
                                     description="Cross-check Tiingo raw closes against "
                                                 "Alpaca before a registered run")
    parser.add_argument("--universe", action="store_true",
                        help="Check the six ADR-011 symbols instead of the 35 of ADR-013")
    args = parser.parse_args(argv)
    symbols = UNIVERSE if args.universe else BROAD_UNIVERSE
    settings = load_settings()
    try:
        universe = load_universe(settings.tiingo_dir, symbols)
        client = PaperClient(load_paper_credentials())
        end = universe.dates[-1]
        results = {symbol: check_symbol(universe.assets[symbol].raw_close,
                                        client.daily_history(symbol, CHECK_START, end),
                                        CHECK_START, end)
                   for symbol in symbols}
        summary = summarize(universe, results)
        path = save_check(summary, settings.data_dir / "checks")
    except (OSError, ValueError) as error:
        return _fail(run, error)
    _emit(run, {"passed": summary["passed"], "failed_symbols": summary["failed_symbols"],
                "universe_sha256": summary["universe_sha256"],
                "start": summary["start"], "end": summary["end"],
                "results": {symbol: {key: item[key] for key in
                                     ("passed", "shared_days", "mismatches")}
                            for symbol, item in summary["results"].items()},
                "file": str(path.resolve())})
    return 0 if summary["passed"] else 1


def ml_main(argv: list[str], run: Run) -> int:
    """The registered machine-learning candidates (ADR-012)."""
    return _study_main(argv, run, "ml", "Evaluate the four registered machine-learning "
                       "candidates against B1 and B2", _ml_study)


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
        epilog=("Use 'gld', 'multi', 'ml', 'broad', 'data-check', 'universe', 'offline', 'stress', 'web', or 'tiingo-import' for the research "
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


def features_main(argv: list[str], run: Run) -> int:
    """Print the versioned, point-in-time feature snapshot for one date."""
    parser = argparse.ArgumentParser(prog="tradenow features",
                                     description="GLD feature snapshot for one date")
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--data", type=Path, help="GLD CSV; defaults to latest Tiingo import")
    args = parser.parse_args(argv)
    try:
        source_bytes, _ = _gld_source(args.data)
        result = snapshot(parse_gld_csv(source_bytes, EquityConfig()), args.date)
    except (OSError, ValueError, InvalidOperation) as error:
        return _fail(run, error)
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
            "tiingo-import": tiingo_main, "gld": gld_main, "notify-test": notify_test_main,
            "features": features_main, "universe": universe_main, "multi": multi_main, "ml": ml_main,
            "broad": broad_main, "data-check": data_check_main}


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
