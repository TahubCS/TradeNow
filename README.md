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
displays price and equity charts, candidate selection, fills, unfilled orders,
contract transitions, risk decisions,
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

The CSV needs `date,contract,open,high,low,close,volume`, MGC contract names in
contiguous blocks, strictly increasing dates, and 180–5,000 daily bars; the file
must be at most 1 MB and UTF-8 encoded. A change in `contract` marks an explicit
roll. Optional `last_trade_date` (ISO date) and `open_time_ct` (ISO timestamp
with `-05:00` or `-06:00` offset) columns enable expiry and session checks.
Dates must precede their contract's `last_trade_date`. For a Monday trading
date, a regular session open is Sunday at 17:00 Chicago time. The input is
validated before any simulation. The report records its original SHA-256,
source filename, contracts, and the three
chronological periods; the saved bars file preserves its exact bytes. Keep
licensed or private data under `data/private/`, which Git ignores. A local file
does not make results market-valid by itself.

At a contract change, the simulator exits the old contract at its last supplied
close with one tick of slippage and commission. It stays flat until the new
contract has enough bars for the SMA window; the price difference between
contracts is never booked as profit or loss. If `last_trade_date` is supplied,
it blocks entries and exits an open position starting five calendar days
before that date. An untradable roll or expiry exit stops the run instead of
assuming a fill. Orders at the next open remain unfilled when volume is zero,
the timestamp is outside regular hours, or the gap from the prior bar exceeds
seven calendar days. The report and dashboard show these events explicitly.

Initial and maintenance margin are **illustrative** fractions of notional,
defaulting to 10% and 8%. The existing notional cap defaults to 50% of equity,
which is stricter than those margin checks for a single position. You can set
`--max-notional-ratio`, `--initial-margin-rate`, and
`--maintenance-margin-rate` on `offline` runs to examine other assumptions.
These are research inputs, not current exchange or broker margin requirements.
The simulator uses regular [CME MGC contract specifications](https://www.cmegroup.com/content/dam/cmegroup/market-regulation/files/gold-futures-and-options-fact-card.pdf)
for its size, tick, and session window; [CME explains that margins change](https://www.cmegroup.com/education/articles-and-reports/understanding-margin-changes).
Daily bars still cannot validate intraday fills, exchange holidays, DST offsets,
delivery procedures, market impact, or brokerage behavior.

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

The synthetic `MGC_SIM` contract has no expiry metadata. Exchange holidays,
partial fills, and live or paper brokerage behavior remain outside this model.

## Spending and trading boundary

- No Databento requests are made, so running this code consumes **zero** data credits.
- No Alpaca or Tradovate integration exists, so running it cannot place a paper or live order.
- Brokerage integration and data purchasing will require separate, explicit work.

The broader architecture and gates are in [docs/README.md](docs/README.md).
