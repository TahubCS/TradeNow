# Data Pipeline

## Goal

Create a reliable and reproducible market-data pipeline before implementing advanced strategies or agents.

## Phase 1 Data

Start with:

- OHLCV bars
- benchmark/index prices
- daily returns
- rolling volatility
- trading calendar information

Do not begin with news, filings, options flow, or alternative data. They increase complexity before the core system is validated.

## Pipeline

```text
Provider
   |
   v
Raw Ingestion
   |
   v
Schema Validation
   |
   v
Normalization
   |
   v
Persistence
   |
   v
Feature Computation
   |
   v
Feature Snapshot
```

## Canonical Market Bar

```json
{
  "symbol": "AAPL",
  "timestamp": "2026-09-25T20:00:00Z",
  "open": 190.21,
  "high": 194.42,
  "low": 189.85,
  "close": 193.76,
  "volume": 52344122
}
```

## Feature Snapshot

```json
{
  "symbol": "AAPL",
  "timestamp": "2026-09-25T20:00:00Z",
  "return_1d": 0.0124,
  "return_5d": 0.0311,
  "sma_20": 190.42,
  "sma_50": 184.75,
  "rsi_14": 61.4,
  "atr_14": 4.21,
  "volatility_20d": 0.23,
  "volume_ratio_20d": 1.18
}
```

## Required Validation

Every ingestion run should detect:

- missing bars,
- duplicate bars,
- unexpected timestamps,
- null OHLCV values,
- negative prices or volume,
- extreme unexplained jumps,
- market-calendar mismatches.

Bad source data should fail loudly rather than silently propagate into features.

## Storage

### PostgreSQL

Use for:

- symbols
- portfolio state
- positions
- orders
- trades
- experiment metadata
- model metadata
- agent decisions
- risk decisions

### Parquet

Use for:

- historical bars
- large feature matrices
- backtest datasets
- offline ML training data

## Versioning

Every feature dataset should track:

- source provider
- ingestion time
- symbol universe
- date range
- feature version
- code commit
- schema version

A backtest is not reproducible unless its exact data and feature versions can be reconstructed.
