# Architecture Decision Log

Use this file to record decisions that materially affect the system.

---

## ADR-001 — Agent Cannot Call Broker Directly

**Status:** Accepted

### Context

LLM and ML components are probabilistic and may produce invalid or unsafe actions.

### Decision

All trading actions must follow:

```text
Agent -> TradeProposal -> RiskEngine -> Portfolio Manager -> Order Manager -> Broker
```

No agent component receives broker credentials or a direct execution tool.

### Consequences

- safer system boundaries,
- easier auditing,
- easier simulation,
- risk rules remain enforceable,
- slightly more implementation complexity.

---

## ADR-002 — Start With Offline Micro Gold Research

**Status:** Accepted

### Context

The first instrument is Micro Gold futures (MGC). The initial work must not place
trades or consume Databento credits.

### Decision

Build and test the data validation, baseline strategy, deterministic risk checks,
and futures simulation with local synthetic data. Do not connect to Databento or a
broker during this phase. A later integration requires an explicit decision about
data use and a broker-specific paper trading gate before any live trading.

### Consequences

- The research loop can be developed and tested with zero external account activity.
- Historical performance and broker behavior remain unverified until real data and
  paper integration are deliberately introduced.

---

## ADR-003 — Local CSV Before Market-Data Integration

**Status:** Accepted

### Decision

The full research cycle may read a locally supplied MGC daily CSV with
contiguous contract blocks, without contacting a data provider. Record the
original file hash and contracts in each report. The dashboard processes
selected files in memory; the CLI saves an exact input copy with its report.

### Consequences

- No Databento credits or brokerage activity are needed for local replay.
- Source accuracy, licensing, exchange calendar, and brokerage behavior remain
  unverified and must be addressed before paper trading.

---

## ADR-004 — Explicit Daily-Bar Futures Assumptions

**Status:** Accepted

### Decision

Treat a contract change in a local CSV as a planned roll: close the old
contract at its last supplied close, charge slippage and commission, and
warm up the strategy on the new contract. Optional last-trade dates trigger
an early exit guard. Optional Chicago-time opening timestamps gate fills to
regular session hours. Use configurable illustrative margin rates, and record
unfilled orders when volume, session, or stale-data checks fail.

### Consequences

- Contract price jumps across a roll do not become portfolio P&L.
- Daily bars cannot verify intraday liquidity, exchange holidays, correct DST
  offsets, delivery procedures, or broker margin and fills. These checks are
  simulation controls rather than proof that an order could trade.

---

## ADR-005 — Add a Cash-Funded GLD Simulation Path

**Status:** Accepted

GLD is the first planned instrument for real historical-data research. A local,
long-only share simulator uses whole shares, deducts purchases from cash, and
marks open shares at the daily close. Signals at a close can fill no earlier than
the next bar's open. Futures margin, rolls, and expiry rules remain confined to
the existing MGC simulator.

This module currently accepts local bars only. It has no Tiingo or brokerage
connection, and its daily-bar fills cannot establish actual execution quality.

---

## ADR-006 — Human-Approved GLD Paper Trading on Alpaca

**Status:** Accepted

### Context

GLD research is ready for a broker-specific paper gate. Paper accounts use
separate credentials, and Alpaca's simulated fills can differ from live execution.

### Decision

Trade GLD only in an Alpaca paper account, with a person approving every order.
The strategy produces a saved plan; `paper-submit --approve <plan_id>` sends it.
Signals use the Tiingo history the strategy was tested on. Alpaca SIP daily bars
only verify that history, because mixing feeds would trade a signal that was
never backtested. Reconciliation against Alpaca runs before every action, and
any mismatch engages a persistent kill switch.

### Alternatives Considered

- Unattended submission: rejected until paper history shows stable behavior.
- Signals from Alpaca bars: rejected because they differ from the backtest source.
- Opening-auction (`opg`) orders: closer to the simulator's open fill, but
  deferred until their paper-account behavior is verified; day orders are used.

### Consequences

- No live endpoint can be configured, and a non-paper account is refused.
- Buys use a 1% limit above the last close, so large upward gaps leave them
  unfilled and positions are slightly smaller than in the simulator.
- Manual trades in the paper account stop the system until they are undone.

---

## ADR-007 — Hashed Local Files Instead of a Database

**Status:** Accepted

### Context

The roadmap lists a database connection (Phase 0) and PostgreSQL/Parquet
persistence (Phase 1). The system holds one symbol's daily bars (about 5,500
rows), one paper ledger, and a few plans and logs per day, all used by one
person on one machine.

### Decision

Keep all state in files under one data directory (`TRADENOW_DATA_DIR`,
`data/private/` by default):

- market data: CSV plus raw provider JSON and a manifest with SHA-256 hashes,
- paper ledger, plans, and kill switch: JSON written atomically (temp file,
  then rename),
- logs and the run log: JSON lines.

Every report records the hash of the data it used, so any result can be
reproduced from the files it names.

### Alternatives Considered

- PostgreSQL: adds a server to install, back up, and migrate, and gives no
  benefit at this data volume.
- Parquet: suits large columnar data. The CSVs here are small, and CSV stays
  readable and diffable.

### Consequences

- No database service to run; backups are a copy of one directory.
- The ledger has a single writer. Two commands run at the same time could
  race; the routine runs them one after another.
- Revisit when any of these holds: more than a handful of symbols, intraday
  bars, several processes writing state at once, or queries across many runs
  that the JSON-lines files make slow.

---

## ADR-008 — Live-Trading Gate: Beat Buy-and-Hold First

**Status:** Accepted

### Context

A strategy that loses to simply holding the asset is worse than doing nothing,
and a backtest can look good by chance or by selection. The current GLD SMA
strategy returned +32% on its holdout against +63% for 50% buy-and-hold, and
beat that benchmark in only 10 of 29 rolling windows. Real money must be
protected from strategies like that.

### Decision

**No real money is traded unless a strategy passes every check below.** Until
then, a low-cost index fund or simply holding GLD is the better choice, and
this system stays a research and paper-trading tool.

The gate is evaluated automatically (`tradenow/live_gate.py`). Its verdict
appears in the `gld` report, `paper-report`, the dashboard, and every
`paper-auto` run, and a desktop notification is sent whenever the verdict
changes.

**Research stage.** Historical and out of sample: only rolling test windows
whose strategy was selected from earlier bars count.

| Check | Requirement |
|---|---|
| R1 Beats full buy-and-hold | Compounded strategy return across all rolling test windows exceeds compounded 100% GLD buy-and-hold over the same windows |
| R2 Beats consistently | Beats buy-and-hold at the same exposure cap in at least 60% of rolling windows |
| R3 Enough trades | At least 30 closed round trips across the rolling test windows |
| R4 Survives costs | R1 still holds with slippage raised to $0.10 per share |

**Forward stage.** Truly unseen: paper trading after the strategy is frozen.
Only the latest unbroken run of paper sessions with the same strategy version
counts. The version is the hash of the strategy code, the selected hypothesis,
and the risk settings, so changing any of them restarts the count.

| Check | Requirement |
|---|---|
| F1 Long enough | At least 252 paper sessions (about one year) |
| F2 Beats full buy-and-hold | Paper equity return exceeds 100% GLD buy-and-hold over the same sessions |
| F3 No worse risk | Paper maximum drawdown no larger than buy-and-hold's |
| F4 Costs as modeled | Mean fill cost against the simulated fill of at most 10 bps, over at least 10 fills |

### What a pass means

A pass is permission to consider live trading, not an instruction to start.
Live trading still requires the Phase 10 prerequisites, a separate decision
recorded here, and minimal capital at first. No live mode exists in the code,
and the gate never enables one.

### Rules that protect the gate

- Thresholds may be tightened, never loosened, without a new ADR that states
  why, written before looking at the results it would change.
- The final GLD holdout has already been viewed, so it never counts as evidence.
- A strategy that fails may be changed and re-tested, but the forward stage
  starts again from zero.

### Consequences

- The current strategy fails R1 to R3, so it stays in paper trading.
- Most strategies are expected to fail. That is the gate working, not a fault.

---

## ADR-009 — Automated Paper Submission (paper-auto)

**Status:** Accepted. Supersedes ADR-006's per-order approval for the paper
account only.

### Context

Phase 6's exit condition is unattended paper trading without state
inconsistencies. ADR-006 required a person to approve every order until paper
history showed stable behavior.

### Decision

`python -m tradenow paper-auto` runs on a Windows Task Scheduler schedule and
reuses the manual path unchanged: kill switch, reconciliation, the Tiingo and
Alpaca data cross-check, risk checks, the saved plan, and the at-most-once
submission. Only the approval step changes: `auto_submit` in `risk.toml`
replaces the person. Each order records whether a person or paper-auto
approved it.

- **Risk settings** live in `risk.toml`, with hard ceilings in code
  (`tradenow/risk_config.py`). An unknown key, a wrong type, a value outside
  its range, or a file that does not parse stops the run. A missing file means
  the defaults, with `auto_submit = false`.
- **Daily loss limit:** a loss since the previous close (Alpaca's
  `last_equity`) at or above the limit sells at the next open and skips one
  session; buying resumes by itself after that. An unknown baseline blocks
  buys (fail closed). The 10% drawdown halt is unchanged and needs
  `paper-resume`.
- **Volume check:** buys are capped at a fraction of the 20-day average volume.
  Exits are never reduced.
- **One run at a time**, enforced by a lock file; a lock older than two hours
  is treated as left by a crash.
- **Morning check** (`--check`) reconciles and reports fills; it never trades.
- **Notifications** are Windows desktop toasts. They are sent through
  PowerShell at its absolute system path, with a fixed script. The text is
  passed in environment variables and never in the command, credentials are
  stripped from the child's environment, and each notice is sent at most
  once a day.
- **Rollout:** start with `auto_submit = false` (a dry run that plans and
  notifies) alongside manual approval for about 5 sessions, then turn it on.

### Alternatives Considered

- Keeping manual approval permanently: rejected, because it cannot meet the
  Phase 6 exit condition.
- Cloud scheduling (GitHub Actions): rejected for now. The ledger and imports
  are local files (ADR-007), and credentials would move to a third party.
  Revisit when the system moves off this PC, starting with an ADR on storing
  keys in the operating system's credential store.
- Broker-side stop orders: deferred. The backtest does not model stops, so
  paper results would stop matching it.

### Consequences

- The PC must be on and logged in during the evening window.
- Phase 6 passes after 20 consecutive unattended sessions with no
  reconciliation failures and no manual repairs, per the run log and
  `paper-report`.
- Automation does not change the live-trading gate (ADR-008). No live mode
  exists.

---

## ADR-010 — Registered GLD Strategy Candidates

**Status:** Accepted. Registered before any of the new candidates was
evaluated on real GLD data.

### Context

The original SMA strategy fails the live-trading gate (ADR-008). Trying many
ideas on the same history until one looks good would find luck, not skill. So
the candidates, their parameters, and the evaluation method are fixed here,
in writing, before the results are known.

### Decision

**The candidate set** (`GLD_CANDIDATES` in `tradenow/strategies.py`), 12 in
total. A test holds this exact list.

| # | Candidate | Rule |
|---|---|---|
| 1-3 | `sma_3_10`, `sma_5_20`, `sma_10_30` | Long when the fast average of closes is above the slow one (the original set) |
| 4-5 | `tsmom_6_1`, `tsmom_12_1` | Long when the 6- or 12-month return, skipping the latest month, is positive |
| 6 | `trend_200` | Long when the close is above its 200-day average |
| 7 | `trend_200_volfilter` | As 6, and only while 20-day volatility is below its 1-year median |
| 8-9 | `donchian_55_20`, `donchian_20_10` | Enter above the prior 55- (or 20-) day high; exit below the prior 20- (or 10-) day low |
| 10-12 | `tsmom_12_1_vt15`, `trend_200_vt15`, `donchian_55_20_vt15` | Rules 5, 6, and 8 with entry size min(1, 15% / 20-day annualized volatility) |

Volatility targeting is registered on three fixed rules. Applying it to
whichever rule looked best would be choosing after seeing the results.

**The method:**

- Selection is unchanged: every window picks the candidate with the highest
  validation return minus maximum drawdown, or cash if none is positive
  (`tradenow/selection.py`). The gate therefore judges the whole process,
  "pick the best of these 12 from past data", not a winner chosen afterwards.
- Research runs use a fully invested position when a strategy is in
  (`RESEARCH_CONFIG`: maximum position 100% of cash, never margin), so timing
  is compared fairly with 100% buy-and-hold. The 10% drawdown halt, $0.01
  slippage per share, and zero commission stay as they are.
- Features (`gld_features_v2`) are computed once from all history. Each day's
  values use only earlier bars, so 6- and 12-month and 200-day lookbacks work
  inside short test windows without seeing the future. The SMA candidates
  read only closes inside each window, as before.
- Entries are sized when opened and are not rebalanced while held.
- Every `gld` run is logged in the experiment log, and the gate reports how
  many candidates were evaluated.

### If a candidate passes the research stage

`paper-auto` already selects among these same 12 candidates. To collect
forward evidence that matches this research, set `max_position_fraction = 1.00`
in `risk.toml`; the forward count restarts under that setting (ADR-008).

### If none passes

Record the result. The next option is multi-asset trend-following, which
needs new data, a new simulator, and a new registration ADR. The other option
is to accept that a low-cost index fund is the better choice.

### Result (2026-09-27)

Evaluated once on the Tiingo GLD history, 5,497 daily bars (experiment
`cc6999fc32cd36f4`). **Verdict: FAIL.** Failing checks: R1, R2, R4. R3
passed.

| Rolling tests before the holdout (29 windows) | Registered process | 100% GLD buy-and-hold |
|---|---|---|
| Compounded return | +15.6% | +158.7% |
| Windows with a positive return | 9 of 29 | — |
| Windows beating same-cap buy-and-hold | 10 of 29 (18 needed) | — |
| Closed trades | 64 | — |

The final holdout selected `trend_200_volfilter`: +59.5% with an 8.3%
maximum drawdown, against +125.6% and 26.4% for buy-and-hold. It was in the
market 36% of the time. Holding about 45% GLD and 55% cash would have had the
same volatility and a similar return, so the timing added little. That
period had already been viewed and is not evidence.

No candidate trades real money. Next step: multi-asset trend-following
(ADR-011).

### Consequences

- Changing, adding, or removing a candidate requires a new ADR, and every
  earlier result stays in the experiment log.
- The final GLD holdout has already been viewed and is never evidence.

---

## ADR-011 — Registered Multi-Asset Trend-Following Research

**Status:** Accepted. Registered before any multi-asset code or result
existed.

### Context

Trend rules on GLD alone failed the gate (ADR-010). Published evidence for
time-series momentum is much stronger across many markets, because assets
that do not move together smooth out each other's whipsaws. This ADR fixes
the assets, rules, sizing, method, and pass criteria before anything is
built or run.

### Universe and data

- **Six ETFs:** GLD (gold), SLV (silver), SPY (US stocks), EFA
  (international stocks), IEF (US government bonds), DBC (broad
  commodities).
- **History:** from the first day all six have a Tiingo bar (spring 2006) to
  the latest import. The six share one calendar; a missing day for any
  symbol is an error, never filled in.
- **Prices:** Tiingo's dividend- and split-adjusted OHLC for signals,
  simulated fills, and valuation, so the results include dividends as total
  return. Raw prices are kept for execution checks.

### Rules and sizing (6 candidates)

Each rule is evaluated per asset at the last close of each month, using the
point-in-time features of ADR-010 computed on adjusted prices:

- **mom:** in when 12-1 month momentum is positive.
- **trend:** in when the close is above its 200-day average.
- **both:** in when both are true.

Two ways to size an asset that is in:

- **eq:** 1/6 of equity.
- **iv35:** inverse 60-day volatility, normalized across all six assets and
  capped at 35% per asset, with any excess shared proportionally among the
  uncapped assets.

Assets that are out hold cash. The candidates are `mom_eq`, `trend_eq`,
`both_eq`, `mom_iv35`, `trend_iv35`, and `both_iv35`.

### Simulation

- Rebalance monthly: signals at the month's last close, orders at the next
  open. An asset trades only if its target differs from its holding by more
  than 1% of equity.
- Whole shares, sells before buys, cash never negative, never margin, total
  weight at most 100%.
- Costs: $0.01 slippage per share, zero commission. The stress test uses
  $0.10.
- The 10% portfolio drawdown halt is checked daily: it sells everything and
  blocks new entries for the rest of the run, as in the GLD research.

### Selection and tests

These are the same as ADR-010. A 60/20/20 chronological split is used, and
rolling windows before the final holdout: 504 development, 126 validation,
and 126 test bars, with test blocks that do not overlap. Each window selects
the candidate with the highest validation return minus maximum drawdown, or
cash if none is positive.

### Benchmarks and the gate (the strictest option)

The strategy must beat **both** benchmarks:

- **B1:** equal-weight buy-and-hold of the same six ETFs, rebalanced
  monthly, with the same costs.
- **B2:** 100% SPY buy-and-hold, the "just buy an index fund" alternative.

| Check | Requirement |
|---|---|
| R1 | Compounded return across the rolling test windows exceeds both B1's and B2's |
| R2 | Beats both B1 and B2 in at least 60% of the rolling windows |
| R3 | At least 30 closed round trips, counted per asset |
| R4 | R1 still holds with slippage at $0.10 per share |
| R5 | Maximum drawdown of the chained rolling test returns is no larger than either benchmark's, chained the same way |
| F1 | At least 252 paper sessions on one frozen strategy version |
| F2 | Paper return exceeds both B1 and B2 over those sessions |
| F3 | Paper maximum drawdown is no larger than either benchmark's |
| F4 | Mean fill cost against the simulated fill of at most 10 bps, over at least 10 fills |

This is deliberately harder than ADR-008's single-asset gate. Trend-following
usually trails stocks in long bull markets and wins by losing less in
crashes, so beating SPY on return over 2006 onward is unlikely. That is
accepted: the rule exists to prevent investing in anything that an index
fund would beat.

### What happens after the run

- The result is recorded here and in the experiment log, pass or fail.
- **If R1 to R5 pass:** multi-asset paper trading is built (a separate ADR),
  the dry run comes first, then a year of forward testing (F1 to F4).
- **If they fail:** stop, or register Phase 7 machine learning on this
  dataset in a new ADR. The candidates and thresholds above are not tuned
  and re-run.

### Clarifications (recorded 2026-09-27, before any multi-asset result)

Written while no multi-asset code had been run on real data. They settle
details the rules above leave open; none loosens the gate.

1. **Sizing at the fill-day open.** On a fill day, equity is valued at the
   open. Target shares for a buy are floor(weight × equity ÷ (open +
   slippage)). The 1% band compares values at the same open prices.
   Real and paper orders must be sized before the open; that is an MA6
   concern.
2. **Not enough cash.** If the buys cost more than the cash left after the
   sells, slippage included, every buy is shrunk by the same fraction and
   then rounded down to whole shares.
3. **Full exits.** A target of 0 always sells the whole position; the 1%
   band does not apply. The drawdown halt also sells everything.
4. **First signal day.** The first bar's close of any run or window is an
   extra signal day for the strategy and both benchmarks, so all of them
   start at the second bar's open. This applies to development,
   validation, and test windows alike.
5. **Benchmarks.** B1 and B2 do not use the 10% halt. B1's monthly
   rebalance uses the same 1% band, whole shares, and costs. B2 buys at
   the first signal and then holds without rebalancing.
6. **Round trips (R3).** One round trip is one asset going from 0 shares to
   held and back to 0. Trims and top-ups do not count, and a position still
   open at the end of a window does not count. A trip's P&L is its net cash
   flow; entry is its first buy and exit its final sell.
7. **Blocked orders.** On a zero-volume day, or after a gap longer than
   `max_order_gap_days` (7 calendar days, as in `equity.py`), that asset's
   order does not fill. Its target persists and the order retries at each
   next open until it fills or a newer signal replaces it. This applies to
   rebalances and halt sales. A retry is evaluated like a first attempt at
   that day's open: re-sized from that day's equity, with the 1% band
   checked again.
8. **Warmup.** An asset whose rule or 60-day volatility cannot be evaluated
   yet is out. iv35 normalizes only across the assets that have a
   volatility value.
9. **Month-end** is the last trading day of each calendar month in the
   aligned calendar.
10. **Fresh start.** Every run or rolling window starts from cash, with no
    halt carried over.
11. **R5 chaining.** The daily equity curves of the rolling test windows
    are joined end to end, each window starting from the previous window's
    ending value, and the maximum drawdown is measured on that daily curve.
    B1 and B2 are chained the same way.
12. **R4 stress.** The whole rolling process, selection included, is run
    again at $0.10 slippage per share, and B1 and B2 also pay $0.10, as in
    ADR-008's GLD stress test.
13. **iv35 order.** Inverse 60-day volatility is taken for every asset that
    has a value and normalized to sum to 1. Any weight above 35% is capped
    and its excess shared among the uncapped assets in proportion to their
    weights, repeated until none exceeds 35%. Assets that are out then hold
    cash. Weights are rounded down to 10 decimal places so they never add
    up to more than 1. Excess that cannot be placed (fewer than three
    assets with a value) stays in cash.
14. **The holdout** (the final 20%) is reported for information only. The
    gate uses only the rolling windows before it. A tie in validation score
    goes to the earlier candidate in the registered order, as in ADR-010.

### Result (2026-09-27)

Evaluated once with `multi` on Tiingo's adjusted history of the six ETFs,
5,135 common days from 2006-04-28 (SLV's first bar) to 2026-09-25
(experiment `5fa2a52477b95cf4`, data `f0881e80…`). **Verdict: FAIL.**
Failing checks: R1, R2, R4. R3 and R5 passed.

| Rolling tests before the holdout (27 windows, Oct 2008 to May 2022) | Registered process | B1 equal weight | B2 SPY |
|---|---|---|---|
| Compounded return | +11.0% | +144.9% | +435.2% |
| Compounded return at $0.10 slippage (R4) | +7.1% | +127.0% | +426.2% |
| Chained maximum drawdown (R5) | 16.7% | 26.1% | 33.7% |
| Windows beaten | — | 8 of 27 | 3 of 27 |

- **Beat both benchmarks in 1 of 27 windows** (17 needed). 33 closed
  round trips (30 needed).
- **Mostly in cash.** In 16 of 27 windows no candidate had a positive
  validation score, so the process held cash. Those windows missed +86% (B1)
  and +207% (B2).
- **Behind when invested, too.** In the 11 windows with a selected
  candidate, the process made +11.0% against +31.6% for B1 and +74.6% for B2
  over the same windows. The low drawdown (R5) comes mainly from sitting in
  cash, not from avoiding losses while invested.
- The rolling tests begin in late October 2008, so most of the 2008 crash,
  where trend-following is known to help, falls in development periods, not
  test blocks. That is a property of the registered method, not a reason
  to re-run.

The holdout (information only, 2022-08-23 to 2026-09-25) selected
`trend_eq`: +54.9% with a 10.6% maximum drawdown, against +95.1% and 13.5%
for B1 and +97.7% and 18.7% for B2.

No candidate trades real money, and multi-asset paper trading (MA6) is not
built. As registered, the candidates and thresholds are not tuned and
re-run. Counting ADR-010, 18 registered candidates across two experiments
have failed. The remaining options are to stop, accepting that a low-cost
index fund is the better choice, or to register Phase 7 in a new ADR before
any work on it.

---

## ADR-012 — Registered Machine-Learning Candidates (Phase 7)

**Status:** Accepted. Registered 2026-09-27, before any Phase 7 code or
result existed.

### Context

Rule-based timing failed on GLD (ADR-010) and on six ETFs (ADR-011). The
owner's goal is live trading by an agent, which ADR-008 allows only after a
strategy beats buy-and-hold historically and in a year of paper trading.
This ADR tests whether models that learn from past patterns can do that.

### Unchanged from ADR-011

Universe, adjusted prices, simulator, costs, monthly signals, the 1% band,
the 10% halt, selection rule, 60/20/20 split, rolling 504/126/126 windows,
benchmarks B1 and B2, and clarifications 1 to 14. The gate is ADR-011's R1
to R5 and F1 to F4, unchanged.

### Data the models learn from

- **Features** (13, scale-free, point-in-time, `gld_features_v2` on
  adjusted prices): ret_1, ret_5, ret_20, ret_60, ret_126, ret_252,
  mom_6_1, mom_12_1, dist_sma_200, rsi_14, vol_20, vol_60, drawdown_252.
- **Samples:** one per asset per month-end where all 13 features exist.
- **Label:** the asset's return from that month-end close to the next
  month-end close, divided by its vol_60 at the start, so that one
  volatile asset does not dominate a model pooled across all six.
- **No look-ahead:** the model sees only the history up to the signal
  day. A month-end is recognized once the next trading day is in that
  history, so a sample is used for training only if its label ended
  before the signal day. A test proves it.

### Training

- At every signal day, one model is fitted on all eligible samples since
  the start of history, pooled across the six assets.
- Features are standardized with the training samples' mean and standard
  deviation only.
- A model needs labels from at least 24 distinct month-ends (and, for
  nearest neighbours, at least 50 samples); until then, every asset is out.

### Candidates (4)

| Candidate | Model (scikit-learn) | In when | Sizing |
|---|---|---|---|
| ridge_eq | Ridge(alpha=1.0) | prediction > 0 | 1/6 |
| ridge_iv35 | Ridge(alpha=1.0) | prediction > 0 | iv35 (ADR-011) |
| knn_eq | KNeighborsRegressor(n_neighbors=50) | prediction > 0 | 1/6 |
| knn_iv35 | KNeighborsRegressor(n_neighbors=50) | prediction > 0 | iv35 (ADR-011) |

Settings are fixed; nothing is tuned after results exist.

### Implementation

scikit-learn 1.9.1, numpy 2.5.3, and scipy 1.18.1, pinned exactly in an
`ml` extra. Floats are used only inside the models; each prediction becomes
a Decimal rounded to 10 places before the in/out decision. Money stays
Decimal. These libraries need Python 3.12 or later, so CI moves from 3.11
to 3.13, the version the owner runs.

### Trials

With ADR-010 (12) and ADR-011 (6), 22 registered candidates in total. The
report states this count.

### What happens after the run

- The result is recorded here and in the experiment log.
- **If R1 to R5 pass:** a new ADR for paper trading the model, then a year
  of forward testing (F1 to F4). Only if those pass, a separate ADR may
  consider live trading, starting with minimal capital; that ADR is the only
  way rule 1 (paper only) can change.
- **If they fail:** stop. The candidates are not tuned and re-run.

---

## ADR Template

### ADR-XXX — Title

**Status:** Proposed / Accepted / Rejected / Superseded

### Context

Describe the problem.

### Decision

Describe the chosen approach.

### Alternatives Considered

Describe credible alternatives.

### Consequences

Describe benefits, tradeoffs, and future constraints.
