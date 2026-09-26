# System Architecture

## Objective

Build a modular trading research and execution system that can:

- ingest and normalize market data,
- calculate features,
- generate deterministic and ML-based signals,
- use an agent to create trade proposals,
- enforce deterministic risk controls,
- simulate and paper trade,
- record every decision for later replay and evaluation.

The agent is a decision-support component, not the final authority over capital.

## High-Level Flow

```text
Market / Fundamental / News Data
              |
              v
        Data Ingestion
              |
              v
        Feature Engine
              |
              v
   Strategy / ML Signal Layer
              |
              v
        Agent Reasoning
              |
              v
        Trade Proposal
              |
              v
         Risk Engine
              |
       approve / reject
              |
              v
       Portfolio Manager
              |
              v
         Order Manager
              |
              v
        Broker Adapter
              |
              v
   Paper or Live Brokerage
```

## System Boundaries

### Data Layer
Responsible for obtaining, cleaning, validating, and storing source data.

### Feature Layer
Transforms raw data into stable and reproducible model/strategy inputs.

### Strategy Layer
Produces signals from deterministic rules or trained models.

### Agent Layer
Combines market state, strategy outputs, retrieved context, and portfolio state into a structured trade proposal.

### Risk Layer
Validates every proposal against hard constraints.

### Portfolio Layer
Tracks positions, cash, realized/unrealized PnL, exposure, and capital allocation.

### Execution Layer
Converts approved portfolio actions into broker-specific orders.

### Evaluation Layer
Measures performance, stability, calibration, failure modes, and operational correctness.

## Non-Negotiable Rule

The agent must never call the broker directly.

```text
Agent -> TradeProposal -> RiskEngine -> OrderManager -> Broker
```

There should be no alternate path.

## Suggested Repository Layout

```text
trading-agent/
├── apps/
│   ├── web/
│   └── api/
├── packages/
│   ├── data/
│   ├── features/
│   ├── strategies/
│   ├── models/
│   ├── agent/
│   ├── risk/
│   ├── portfolio/
│   ├── execution/
│   ├── backtesting/
│   └── evaluation/
├── docs/
├── tests/
├── notebooks/
└── scripts/
```

## Initial Technology Choices

- Python for quantitative, ML, agent, and execution logic
- FastAPI for backend APIs
- PostgreSQL for relational state
- Parquet for historical analytical datasets
- Pandas or Polars for feature computation
- scikit-learn / XGBoost / LightGBM for early ML
- PyTorch only when a neural model has a justified use case
- Next.js + TypeScript for the dashboard
- Docker for local reproducibility
