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
