"""Run the offline-only research example."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .market_data import load_bars
from .simulation import Config, simulate


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay local MGC bars without network or broker access")
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

