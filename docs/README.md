# Trading Agent Documentation

This folder defines the architecture, constraints, development plan, and operating rules for the trading-agent project.

The system is intentionally designed so that no language model or ML model can place trades directly. Every proposed trade must pass through deterministic portfolio and risk controls before it can reach an execution adapter.

## Core Principles

1. **Research before automation**  
   The system must first prove that its data, signals, backtests, and evaluation process are reliable.

2. **Deterministic risk controls**  
   Risk limits are code-level rules and cannot be overridden by an agent.

3. **Replayability**  
   Every market snapshot, model output, agent decision, risk decision, order, and result should be reproducible.

4. **Separation of concerns**  
   Data ingestion, features, strategies, ML, agents, risk, portfolio management, execution, and evaluation remain independent modules.

5. **Paper trading before live trading**  
   The system must demonstrate stable behavior in historical and paper environments before any live broker connection is enabled.

## Documentation Map

- `architecture.md` — system components and boundaries
- `data-pipeline.md` — data ingestion, storage, normalization, and features
- `strategy.md` — deterministic and ML strategy design
- `agent.md` — LLM agent responsibilities and restrictions
- `risk.md` — portfolio and trade risk controls
- `backtesting.md` — historical simulation rules
- `execution.md` — paper/live brokerage execution architecture
- `evaluation.md` — metrics, experiment tracking, and failure analysis
- `roadmap.md` — phased implementation plan
- `decisions.md` — architectural decision log template
