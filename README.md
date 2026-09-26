# Tradenow

This is the first, **offline-only** research slice for Micro Gold futures (MGC).
It reads a bundled synthetic CSV, validates the bars, runs a simple moving-average
baseline, applies a deterministic exposure limit, and simulates fills and futures
profit and loss. It cannot submit brokerage orders or contact Databento. There are
no API clients, credentials, or network calls in this project.

## Run locally

Requires Python 3.11 or newer. No packages need to be installed.

```powershell
python -m tradenow
python -m unittest discover -s tests -v
```

The command prints a JSON experiment record. Its `mode` is always
`offline_simulation`, and the bundled bars are fictional; results have no trading
significance. To replay a different **local** CSV, use `python -m tradenow --data
path/to/bars.csv`. The required columns are `date,contract,open,high,low,close,volume`.
All rows must refer to one contract, sorted by date. This first slice does not
handle futures rolls, margin, intraday data, or real market feeds.

## Spending and trading boundary

- No Databento requests are made, so running this code consumes **zero** data credits.
- No Alpaca or Tradovate integration exists, so running it cannot place a paper or live order.
- Brokerage integration and data purchasing will require separate, explicit work.

The broader architecture and gates are in [docs/README.md](docs/README.md).

