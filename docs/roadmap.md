# Development Roadmap

## Phase 0 — Foundation

Deliverables:

- repository structure,
- configuration system,
- logging,
- environment management,
- database connection,
- test framework,
- architecture documentation.

Exit condition:

The project runs locally with deterministic configuration and basic CI.

## Phase 1 — Market Data

Deliverables:

- one market-data provider,
- OHLCV ingestion,
- normalized schema,
- PostgreSQL/Parquet persistence,
- data-quality checks.

Exit condition:

Historical bars can be fetched, stored, validated, and replayed.

## Phase 2 — Features

Deliverables:

- return features,
- SMA,
- RSI,
- ATR,
- rolling volatility,
- volume features,
- feature versioning.

Exit condition:

A reproducible feature snapshot can be generated for any supported timestamp.

## Phase 3 — Baseline Strategy

Deliverables:

- one deterministic strategy,
- strategy interface,
- signal logging,
- basic portfolio simulator.

Exit condition:

Signals can be generated and replayed from historical data.

## Phase 4 — Backtesting

Deliverables:

- event loop,
- transaction-cost model,
- slippage model,
- metrics,
- experiment logging.

Exit condition:

The same strategy produces identical results from the same dataset and configuration.

## Phase 5 — Risk and Portfolio

Deliverables:

- position limits,
- exposure limits,
- drawdown controls,
- position sizing,
- kill switch,
- risk decision logging.

Exit condition:

Unsafe trade proposals are consistently rejected.

## Phase 6 — Paper Trading

Deliverables:

- broker abstraction,
- paper broker integration,
- order state machine,
- reconciliation,
- monitoring dashboard.

Exit condition:

The system can run unattended in paper mode without state inconsistencies.

## Phase 7 — ML

Deliverables:

- training dataset pipeline,
- baseline logistic model,
- tree-based model,
- walk-forward evaluation,
- calibration analysis.

Exit condition:

Model value is demonstrated out of sample against deterministic baselines.

## Phase 8 — Agent

Deliverables:

- structured agent inputs,
- structured proposal schema,
- evidence grounding,
- confidence output,
- no-trade behavior,
- agent evaluation harness.

Exit condition:

Agent proposals are reproducible enough to evaluate and cannot bypass controls.

## Phase 9 — Context Expansion

Possible additions:

- news retrieval,
- earnings data,
- SEC filings,
- macro indicators,
- event extraction.

Only add sources that can be timestamped and historically reproduced.

## Phase 10 — Controlled Live Trading

Prerequisites:

- a strategy that passes the live-trading gate (ADR-008): it must beat plain
  buy-and-hold out of sample, after costs, over enough trades, in both
  historical research and at least one year of forward paper trading,
- stable paper-trading history,
- reconciliation tested,
- risk rules tested,
- order idempotency tested,
- emergency halt tested,
- live mode opt-in only.

Initial live deployment should use minimal capital and restricted instruments.
