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
