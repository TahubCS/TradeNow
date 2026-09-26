"""Run the offline-only research example."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .market_data import load_bars
from .offline import run_offline, save_offline
from .simulation import Config, simulate


def offline_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the complete synthetic-data research cycle")
    parser.add_argument("--seed", type=int, default=3)
    parser.add_argument("--days", type=int, default=360)
    parser.add_argument("--output", type=Path, default=Path("artifacts/offline"))
    args = parser.parse_args(argv)
    try:
        report, csv_text = run_offline(args.seed, args.days)
        bars_path, report_path, markdown_path = save_offline(report, csv_text, args.output)
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(json.dumps({"mode": report["mode"], "run_id": report["run_id"],
                      "selected_hypothesis": report["research"]["selected_hypothesis"],
                      "holdout_summary": report["research"]["holdout_summary"],
                      "bars_file": str(bars_path.resolve()),
                      "report_file": str(report_path.resolve()),
                      "readable_report": str(markdown_path.resolve())}, indent=2))
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "offline":
        return offline_main(sys.argv[2:])

    parser = argparse.ArgumentParser(
        description="Replay local MGC bars without network or broker access",
        epilog="For the complete generated-data workflow, run: python -m tradenow offline",
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
