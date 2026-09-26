# Tradenow

This is a complete **offline research workflow** for a fictional Micro Gold-shaped
contract (`MGC_SIM`). It generates deterministic market bars, validates them,
evaluates fixed trade hypotheses, selects one on validation data, and replays the
holdout period through proposals, risk checks, a simulated order manager, and
portfolio accounting. It cannot submit brokerage orders or contact Databento.
There are no API clients, credentials, or network calls in this project.

## Run locally

Requires Python 3.11 or newer. No packages need to be installed.

```powershell
python -m tradenow offline
python -m tradenow offline --seed 7 --days 360
python -m tradenow
python -m unittest discover -s tests -v
```

`python -m tradenow offline` is the main workflow. It saves generated bars, a full
JSON audit record, and a readable Markdown report under `artifacts/offline/`.
The default seed exercises selection and simulated fills; seed 7 demonstrates the
no-trade selection gate. The fixed candidates are SMA 3/10, 5/20, and 10/30.
Each chronological period starts flat. The validation score is return percentage
minus maximum drawdown percentage. A nonpositive best score selects no trade.
The holdout period is never used to select a candidate. None of these synthetic
results say anything about expected performance in the gold market.

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
