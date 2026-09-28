# Tradenow

This is an **offline research workflow** for gold markets: synthetic Micro Gold
futures bars and locally imported GLD ETF history. By default it generates a
fictional contract (`MGC_SIM`), validates its bars,
evaluates fixed trade hypotheses, selects one on validation data, and replays the
holdout period through proposals, risk checks, a simulated order manager, and
portfolio accounting. The research commands cannot submit brokerage orders or
contact Databento. The separate, manual `tiingo-import` command fetches GLD
end-of-day ETF data when explicitly run, and the `paper-*` commands trade GLD in
an Alpaca **paper** account only after a person approves each order.

## Live-trading gate: read this first

**No real money is traded unless a strategy passes the live-trading gate
([ADR-008](docs/decisions.md)).** It must beat plain buy-and-hold out of sample,
after costs, over enough trades, both in historical rolling tests and in at
least one year of forward paper trading. Until a strategy passes, a low-cost
index fund or simply holding GLD would very likely do better than anything this
system trades. The current SMA strategy fails the gate.

The system checks the gate for you. `python -m tradenow gld`, `paper-report`,
`paper-auto`, and the dashboard all show its verdict, and `paper-auto` sends a
desktop notification whenever the verdict changes.

## Run locally

Requires Python 3.11 or newer. No packages need to be installed.

```powershell
python -m tradenow offline
python -m tradenow offline --seed 7 --days 360
python -m tradenow stress
python -m tradenow
python -m unittest discover -s tests -v
```

### Development checks

The linter and type checker are pinned in the `dev` extra. CI
(`.github/workflows/ci.yml`) runs the same three checks on Ubuntu and Windows
for every push and pull request:

```powershell
python -m pip install -e ".[dev,web]"
ruff check tradenow tests
mypy
python -m unittest discover -s tests -v
```

### Settings, logs, and the run log

All private state lives under one data directory, `data/private/` by default:
Tiingo imports in `tiingo/`, the paper ledger and plans in `alpaca/`, and logs
in `logs/`. Set `TRADENOW_DATA_DIR` to move all of it together; a relative
path is resolved against the project root. `TRADENOW_MODE` may only be
`paper`, and any other value stops every command before it runs.

Every command appends exactly one line to `logs/runs.jsonl`: a run ID, the
command, start and finish times, its outcome (`OK`, `BLOCKED` with the refusal
code, `ERROR`, `USAGE`, or `CRASH`), and identifying fields such as the plan ID,
client order ID, or imported data hash. Detailed log records go to
`logs/tradenow.jsonl` (rotated at 5 MB), and a one-line summary is printed to
stderr, so stdout still carries only each command's JSON result. Tiingo and
Alpaca credentials are redacted from everything written.

## Import GLD daily history

Place `TIINGO_API_KEY=your_key` in the project-root `.env.local`, or set the
`TIINGO_API_KEY` environment variable. Both the file and downloaded data under
`data/private/` are ignored by Git. Do not commit or share Tiingo bars; Tiingo's
[free EOD plan](https://www.tiingo.com/about/pricing) is for internal use.

Run one manual import with explicit dates:

```powershell
python -m tradenow tiingo-import --start 2004-11-18 --end 2026-09-25
```

This command makes at most two Tiingo EOD requests for `GLD` and never retries
automatically. It checks ticker coverage, validates daily bars, and saves raw
JSON, a CSV containing raw and adjusted prices, and a source manifest under
`data/private/tiingo/`. Repeating a date range that is already complete stops
before any API request. If an earlier import of the same range ends before the
requested end date (for example, it ran before Tiingo published that day's
close), running it again fetches the range once more. It replaces the files only
when Tiingo now has a newer bar and every earlier raw bar is unchanged. The
earlier files move to `data/private/tiingo/superseded/`. With no newer bar yet,
the result is `UNCHANGED` and nothing is written. Revised history is refused,
and the earlier import is kept. The key is sent in an authorization header and never written to the
data files. The import command does not run a backtest or contact a broker.

For the multi-asset research (ADR-011), import all six registered ETFs (GLD,
SLV, SPY, EFA, IEF, DBC) with the same protections, then check that they align:

```powershell
python -m tradenow tiingo-import --universe --start 2004-11-18 --end 2026-09-25
python -m tradenow universe
```

`--symbols SPY,IEF` imports a chosen few. Several symbols make two requests
each; the first failure (for example a rate limit) stops the run and reports
what was already imported. After every import, only the newest three imports
of that symbol are kept, which caps disk use. `universe` uses dividend-adjusted
prices, trims the six histories to the dates they all cover, and refuses any
missing day inside that range.

Replay the newest private GLD import without another Tiingo request:

```powershell
python -m tradenow gld
```

Each report compares the strategy with 50% and 100% buy-and-hold using return,
volatility, Sharpe, Sortino, drawdown, Calmar, exposure, and trade statistics,
and each distinct run is added once to `data/private/experiments.jsonl`, so
every strategy ever tried is counted.

`python -m tradenow features --date YYYY-MM-DD` prints the versioned feature
snapshot for any day (returns, momentum, moving averages, RSI, ATR,
volatility, volume z-score, Donchian channels, drawdown). Each value uses only
data up to that day's close; a test proves no future bar is ever used.

Use `python -m tradenow gld --data path/to/GLD.csv` for another local Tiingo-format
CSV. GLD research chooses among the 12 registered candidates of ADR-010 (SMA
crossovers, time-series momentum, 200-day trend with and without a
volatility filter, Donchian breakouts, and volatility-targeted versions),
using the chronological development/validation/holdout selection. It runs at
the registered research setting (a full position when in, never margin), with
cash-funded whole shares and raw prices. It rejects files with
dividends, splits, or adjusted prices that differ from raw prices until those
cash flows are modeled. Reports go to `artifacts/gld/`; downloaded bars stay
under `data/private/`. Historical results do not establish expected returns.

The GLD 10% drawdown threshold checks closing equity, blocks new entries after
a breach, and requests a sale at the next tradable open. It is not a guaranteed
stop: gaps or blocked fills can push drawdown beyond 10%, and a final-bar breach
leaves an exit pending. Reports and the dashboard show the trigger and exit state.

The GLD report compares the selected holdout strategy with cash and buy-and-hold
at 50% and 100% allocation. It reports calendar-time annualized returns, gross
traded notional relative to starting cash, and holdout results with $0.01,
$0.05, and $0.10 per-share slippage (with the selected strategy fixed). Rolling
checks use 504 development, 126 validation, and 126 non-overlapping test bars,
ending before the final holdout. Each test starts flat; the cash benchmark
assumes zero interest. The strategy warms up its SMA inside each rolling test,
while buy-and-hold enters at the first open. These checks expose sensitivity
and stability, not live trading performance.

## Alpaca paper trading (GLD)

The `paper-*` commands follow the strategy selected by the GLD research in a
simulated Alpaca paper account. Alpaca paper fills are simulated and can differ
from live execution. Live trading is not supported: only the
`https://paper-api.alpaca.markets` endpoint is accepted, and every command
checks that Alpaca reports a paper (`PA…`) account.

Put paper-only keys in the project-root `.env.local` (see `.env.example`):

```text
ALPACA_PAPER_ENDPOINT=https://paper-api.alpaca.markets
ALPACA_PAPER_KEY_ID=...
ALPACA_PAPER_SECRET_KEY=...
```

The endpoint may also be written with `/v2`, as Alpaca's dashboard shows it.

Daily routine, after Tiingo publishes the close (evening, New York time):

```powershell
python -m tradenow tiingo-import --start 2004-11-18 --end 2026-09-28
python -m tradenow paper-plan
python -m tradenow paper-submit --approve <plan_id>   # only if the plan has an order
python -m tradenow paper-status                       # any time; read-only
python -m tradenow paper-report                       # any time; local files only
```

- **`paper-plan`** reconciles first, selects the strategy on validation data
  exactly as `gld` does, then computes the SMA signal from the Tiingo import.
  Alpaca's consolidated (SIP) daily bars are an independent check, not a
  signal input: planning stops if Alpaca has a newer session than Tiingo, the
  session dates differ, or any close in the last three weeks differs by more
  than 0.5%. The plan is saved under `data/private/alpaca/plans/`; nothing is sent.
- **`paper-submit --approve <plan_id>`** is the approval step. It sends the plan's
  order only while the market is closed and before the session the plan was made
  for. Changing a saved plan invalidates its ID. The order is written to the
  local ledger before it is sent. It has a deterministic client order ID, so a
  repeated or timed-out submit is looked up instead of sent again.
- **`paper-halt --reason "..." [--flatten]`** is the kill switch. It first writes
  `data/private/alpaca/kill_switch.json` (so it works even if Alpaca is
  unreachable), then cancels every open order, and with `--flatten` sells the
  whole GLD position at market. While engaged, `paper-plan` and `paper-submit`
  refuse to run.
- **`paper-resume --confirm`** clears the kill switch only if the ledger and
  Alpaca agree.

Reconciliation runs before every plan and submit. Alpaca is the source of truth:
a GLD position different from the ledger's filled orders, any other symbol, an
open order this system did not send, or an unresolved submission engages the
kill switch. Trades made by hand in the paper account therefore stop the system
until they are undone.

Orders match the simulator as closely as a broker allows: whole shares, day
orders, a buy sized from **cash** (never margin buying power) at 50% of cash,
and sells at market. Buys are limit orders 1% above the last close, which
sizes them slightly smaller than the simulator. If GLD gaps up more than 1%, a
buy stays unfilled where the simulator would have bought at the open. A day
order that does not fill at the open can still fill later in the session.

The 10% drawdown rule is the simulator's rule, applied to paper-account
equity: the highest equity seen at planning time is the peak. A breach blocks
new entries permanently and plans a market sale for the next open. As in the
backtest, this is not a guaranteed stop: an overnight gap can make the loss
larger. To start a fresh paper run, reset the Alpaca paper account and delete
`data/private/alpaca/`.

If `tiingo-import` runs before Tiingo publishes the latest close, `paper-plan`
reports `TIINGO_STALE`. Run the same `tiingo-import` command again later; it
refreshes the incomplete import (see above).

**`paper-report`** measures execution quality offline, without contacting
Alpaca. Each order records the plan's reference close, when it was sent, and
Alpaca's submission and fill times. Each fill is compared with three prices:
the plan's close, the session's actual open from the Tiingo history, and the
backtest's simulated fill (that open plus or minus its per-share slippage).
Slippage is reported in basis points, where positive is a cost. The report
also covers fill and rejection rates, partial fills, buy limits that were below
the open, time from the open to the fill, dollar cost against the simulation,
and run-log health: outcomes, block codes, reconciliation failures, and
repeated submits. A fill's open-based comparisons appear after the next Tiingo
import includes its session. The dashboard's paper view shows the same summary.

## Automated paper trading (paper-auto)

`paper-auto` runs the daily routine without you: it imports the close, plans,
sends the order (only if you allow it), measures execution, and checks the
live-trading gate. It uses the same safety path as the manual commands, and
only the Alpaca **paper** account can be reached ([ADR-009](docs/decisions.md)).

1. Copy `risk.example.toml` to `data/private/risk.toml` and adjust it. Every
   value has a hard ceiling in code; a typo, an unknown key, or a value over a
   ceiling stops the run instead of trading.
2. Try it by hand. With `auto_submit = false` (the default) it only plans:

   ```powershell
   python -m tradenow paper-auto            # evening: import, plan, maybe submit
   python -m tradenow paper-auto --dry-run  # never submits, whatever risk.toml says
   python -m tradenow paper-auto --check    # morning: reconcile and report fills
   ```

3. Schedule it on Windows (runs as you, without administrator rights, only
   while you are logged on):

   ```powershell
   .\scripts\schedule-windows.ps1 -Python C:\path\to\python.exe
   .\scripts\schedule-windows.ps1 -Remove   # to stop
   ```

   Evening runs repeat every 30 minutes from 6:30 to 10:30 pm Eastern on
   weekdays. The first run after Tiingo publishes the close does the work, and
   later runs exit quickly. The morning check runs at 10:00 am Eastern.
4. Run about 5 sessions as a dry run next to your manual approvals, then set
   `auto_submit = true`.

Risk rules applied every evening:

- **Daily loss limit** (default 2%): a loss since the previous close sells at
  the next open and skips one session, then buying resumes. If Alpaca does not
  report the previous close's equity, buys are blocked.
- **Drawdown halt** (default 10%): sells, and blocks buys until
  `paper-resume --confirm`.
- **Position and volume caps:** cash only, never margin; a buy is at most 1% of
  GLD's 20-day average volume.
- **Kill switch:** `paper-halt` stops every scheduled run until you resume.

Check notifications with `python -m tradenow notify-test`: it shows a test
notification and prints what Windows did, or the exact error. If nothing
appears, check that Do Not Disturb is off, and that notifications from
"Windows PowerShell" are allowed under Settings → System → Notifications.

You get a Windows desktop notification when an order is sent or finishes, when
a run is blocked or cannot start (at most once a day each), and when the
live-trading gate verdict changes. Every run is also in
`data/private/logs/runs.jsonl` and on the dashboard. The order-by-order record
states whether you or paper-auto approved each order.

## Fixed-mix mode (ADR-015)

Every registered strategy failed the live-trading gate, so the agent can
instead hold a fixed mix of ETFs that **you** choose, in the Alpaca paper
account. It does not try to beat the market; it keeps your mix on target,
checks the data and the account every day, and alerts you. It never uses
margin, never sells short, and never sells automatically after a loss.

1. **See how a mix behaved** (information only; pick by the drops you could
   live through, not by the best past return):
   ```powershell
   python -m tradenow mix-preview --targets SPY=0.6,AGG=0.4
   ```
   Import any ETF that is missing first, with
   `tiingo-import --symbols ... --start 2006-01-01 --end <last session>`.
2. **Write your mix** in `data\private\mix.toml` (format in
   `mix.example.toml`).
3. **Reset the Alpaca paper account** on alpaca.markets to the balance you
   would realistically invest. The first mix run requires an empty account
   and then marks it as mix mode; the GLD paper commands refuse from then on.
4. **Dry run:** `python -m tradenow mix-plan` shows the orders it would send.
5. **Schedule it:** `.\scripts\schedule-windows.ps1 -Mix` replaces the GLD
   tasks with `mix-auto` (evening) and `mix-auto --check` (morning). With
   `auto_submit = false` in `risk.toml` it only plans; set it to `true` after a
   few dry-run evenings.

Rebalancing happens once a quarter (and on the first run, or after you edit
the mix): an ETF trades only when it is more than 5 percentage points of
equity away from its target. Sells go out one evening and buys on a later
one, from the cash actually in the account. `mix-status`, `mix-report`,
`mix-halt --reason ...`, and `mix-resume --confirm` complete the set.

## Local dashboard

Install the optional web server once, then open the dashboard in your browser:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[web]"
.\.venv\Scripts\python.exe -m tradenow web
```

Visit `http://127.0.0.1:8000`. The server binds to your computer only. The
page follows your system's light or dark setting and works down to phone width.

- **Research** replays the latest imported GLD history by default (no API
  request), or synthetic MGC bars, a local MGC/GLD CSV, or the stress suite. It
  shows holdout stat tiles, the drawdown-halt outcome (with the reminder that it
  is not a guaranteed stop), price and equity charts with fill markers, the
  benchmark, slippage, and rolling-window evaluation for GLD, candidate
  selection with the development/validation/holdout timeline, fills, risk
  decisions, and unfilled orders. Hover or use the arrow keys on a chart to
  read values. Browser-selected files are sent only to the local server,
  analysed in memory, and not saved. This tab makes no brokerage, Databento,
  or Tiingo calls.
- **Paper trading** (`#paper`) is a read-only view of the Alpaca paper account:
  kill-switch state, equity, cash, position, market clock, drawdown against the
  10% limit, reconciliation, the latest saved plan with its approve command,
  and recent orders. It calls Alpaca's read-only endpoints only when the tab is
  opened or refreshed. It cannot plan, submit, cancel, or change the kill
  switch; those stay in the CLI.

The API limits file sizes, seeds, and bar counts so accidental requests remain
bounded. Stop the service with Ctrl+C. Uvicorn is the only optional runtime
dependency; the research commands above remain standard-library-only.

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

- No Databento requests are made, so running this code consumes **zero** Databento credits.
- Only `tiingo-import` calls Tiingo, using the free EOD GLD endpoints when run manually.
- Only `paper-submit` and `paper-halt --flatten` can place orders, and only in an
  Alpaca paper account. `paper-plan`, `paper-status`, and `paper-resume` call
  Alpaca read-only. There is no live-trading or Tradovate integration.
- Brokerage integration and data purchasing will require separate, explicit work.

The broader architecture and gates are in [docs/README.md](docs/README.md).
