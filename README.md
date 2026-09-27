# Tradenow

This is an **offline research workflow** for Micro Gold-shaped daily bars. By
default it generates a fictional contract (`MGC_SIM`), validates its bars,
evaluates fixed trade hypotheses, selects one on validation data, and replays the
holdout period through proposals, risk checks, a simulated order manager, and
portfolio accounting. It cannot submit brokerage orders or contact Databento.
There are no broker or market-data API clients, credentials, or outbound calls.

## Run locally

Requires Python 3.11 or newer. No packages need to be installed.

```powershell
python -m tradenow offline
python -m tradenow offline --seed 7 --days 360
python -m tradenow stress
python -m tradenow
python -m unittest discover -s tests -v
```

## Local dashboard

Install the optional web server once, then open the dashboard in your browser:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[web]"
.\.venv\Scripts\python.exe -m tradenow web
```

Visit `http://127.0.0.1:8000`. The server binds to your computer only. It runs
the same research cycle on generated bars or a CSV selected in the browser and
displays price and equity charts, candidate selection, fills, risk decisions,
and the stress suite. Browser-selected files are sent only to the local server,
analyzed in memory, and not saved by the dashboard. There are no brokerage
endpoints, Databento calls, or remote assets. The API limits file sizes, seeds,
and bar counts so accidental requests remain bounded. Stop
the service with Ctrl+C. Uvicorn is the only optional runtime dependency; the
research commands above remain standard-library-only.

`python -m tradenow offline` is the synthetic workflow. It saves generated bars, a full
JSON audit record, and a readable Markdown report under `artifacts/offline/`.
The default seed exercises selection and simulated fills; seed 7 demonstrates the
no-trade selection gate. The fixed candidates are SMA 3/10, 5/20, and 10/30.
Each chronological period starts flat. The validation score is return percentage
minus maximum drawdown percentage. A nonpositive best score selects no trade.
The holdout period is never used to select a candidate. None of these synthetic
results say anything about expected performance in the gold market.

To run the full workflow on a local file, use:

```powershell
python -m tradenow offline --data data/private/mgc-history.csv
```

The CSV needs `date,contract,open,high,low,close,volume`, one MGC contract,
strictly increasing dates, and 180–5,000 daily bars; the file must be at most
1 MB and UTF-8 encoded. The input is validated before any simulation. The
report records its original SHA-256, source filename, contract, and the three
chronological periods; the saved bars file preserves its exact bytes. Keep
licensed or private data under `data/private/`, which Git ignores. A local file
does not make results market-valid by itself: contract rolls, margin, calendar,
and brokerage behavior remain outside the model.

`python -m tradenow stress` replays 12 seeds twice and audits every development,
validation, and holdout simulation. It also injects missing weekdays, extreme
synthetic price jumps, duplicate bars, order retries, a broken fill ledger, an
oversized proposal, and an adverse price gap. It writes JSON and Markdown reports
under `artifacts/stress/` and exits with a failure code if a check fails. Use
`--seeds` and `--days` to change the run size. The 10% jump threshold is a check
for this fictional feed, not a rule for real market data.

`python -m tradenow` keeps the original short sample replay. It prints a JSON
experiment record with daily equity, closed-trade P&L,
total return, maximum close-to-close drawdown, and every risk decision. Its `mode` is always
`offline_simulation`, and the bundled bars are fictional; results have no trading
significance. To replay a different **local** CSV, use `python -m tradenow --data
path/to/bars.csv`. The required columns are `date,contract,open,high,low,close,volume`.
All rows must refer to one contract, sorted by date.

The offline proposal generator is deterministic. It records the two moving averages
as evidence and can decide `NO_TRADE`; it is not an LLM or trained model. An entry
is checked against a maximum notional fraction before reaching the in-memory broker.
The broker applies one tick of slippage and a commission per side, deduplicates order
IDs, and reconciles its position with its fill ledger after every bar.

The drawdown limit defaults to 10% of the highest simulated closing equity. When
breached, it permanently blocks new entries for that run and targets an exit at the
**next bar's open**. A final-bar breach remains open because no next bar exists;
`open_contracts` shows this explicitly. This is a daily-bar research rule, not an
intraday stop order.

The synthetic contract never expires. Futures rolls, margin, exchange holidays,
partial fills, and live or paper brokerage behavior are outside this offline model.

## Spending and trading boundary

- No Databento requests are made, so running this code consumes **zero** data credits.
- No Alpaca or Tradovate integration exists, so running it cannot place a paper or live order.
- Brokerage integration and data purchasing will require separate, explicit work.

The broader architecture and gates are in [docs/README.md](docs/README.md).
