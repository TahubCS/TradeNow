"""Run the offline-only research example."""

import argparse
import hashlib
import json
import sys
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .market_data import load_bars
from .offline import MAX_LOCAL_CSV_BYTES, run_local_csv, run_offline, save_offline
from .simulation import Config, simulate
from .stress import run_stress, save_stress
from .tiingo import import_gld, load_api_key


def offline_main(argv: list[str]) -> int:
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
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(json.dumps({"mode": report["mode"], "run_id": report["run_id"],
                      "data_source": report["data"]["source"],
                      "contract": report["data"]["contract"],
                      "contracts": report["data"]["contracts"],
                      "source_sha256": report["data"]["sha256"],
                      "selected_hypothesis": report["research"]["selected_hypothesis"],
                      "holdout_summary": report["research"]["holdout_summary"],
                      "bars_file": str(bars_path.resolve()),
                      "report_file": str(report_path.resolve()),
                      "readable_report": str(markdown_path.resolve())}, indent=2))
    return 0


def stress_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run synthetic scenario and fault checks")
    parser.add_argument("--seeds", type=int, default=12)
    parser.add_argument("--days", type=int, default=360)
    parser.add_argument("--output", type=Path, default=Path("artifacts/stress"))
    args = parser.parse_args(argv)
    try:
        report = run_stress(args.seeds, args.days)
        json_path, markdown_path = save_stress(report, args.output)
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(json.dumps({"mode": report["mode"], "run_id": report["run_id"],
                      "passed": report["passed"],
                      "seed_runs": len(report["seed_runs"]),
                      "fault_cases": len(report["fault_cases"]),
                      "coverage": report["coverage"],
                      "report_file": str(json_path.resolve()),
                      "readable_report": str(markdown_path.resolve())}, indent=2))
    return 0 if report["passed"] else 1


def web_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Serve the local read-only simulation dashboard")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        import uvicorn
    except ModuleNotFoundError:
        print('Uvicorn is required for the dashboard: python -m pip install -e ".[web]"',
              file=sys.stderr)
        return 1
    uvicorn.run("tradenow.web:app", host="127.0.0.1", port=args.port)
    return 0


def tiingo_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Manually import private GLD daily data from Tiingo")
    parser.add_argument("--start", required=True, type=date.fromisoformat,
                        help="First requested date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, type=date.fromisoformat,
                        help="Last requested date (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    try:
        result = import_gld(args.start, args.end, load_api_key())
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "offline":
        return offline_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "stress":
        return stress_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "web":
        return web_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "tiingo-import":
        return tiingo_main(sys.argv[2:])

    parser = argparse.ArgumentParser(
        description="Replay local MGC bars without network or broker access",
        epilog="Use 'offline', 'stress', 'web', or 'tiingo-import' for the research tools.",
    )
    parser.add_argument("--data", type=Path,
                        default=Path(__file__).resolve().parent.parent / "sample_data" / "mgc_synthetic.csv",
                        help="Path to a local CSV with one futures contract")
    args = parser.parse_args()

    try:
        source = args.data.read_bytes()
        bars = load_bars(args.data)
        result = simulate(bars)
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    result["source_sha256"] = hashlib.sha256(source).hexdigest()
    result["config"] = {key: str(value) for key, value in vars(Config()).items()}
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
