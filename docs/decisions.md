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

### Consequences

- Changing, adding, or removing a candidate requires a new ADR, and every
  earlier result stays in the experiment log.
- The final GLD holdout has already been viewed and is never evidence.

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
