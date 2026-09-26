# Evaluation and Experiment Tracking

## Objective

Evaluate the system at multiple layers instead of judging it only by portfolio return.

## Layer 1: Data Quality

Track:

- missing data,
- stale data,
- duplicates,
- schema violations,
- feature calculation failures.

## Layer 2: Strategy Quality

Track:

- signal frequency,
- hit rate,
- expected return,
- drawdown,
- turnover,
- regime sensitivity.

## Layer 3: Model Quality

Track:

- accuracy where appropriate,
- precision / recall,
- ROC-AUC,
- calibration,
- Brier score,
- expected value by probability bucket.

## Layer 4: Agent Quality

Track:

- valid proposal rate,
- grounded evidence rate,
- unsupported claims,
- contradictions,
- attempted risk violations,
- confidence calibration,
- no-trade frequency.

## Layer 5: Portfolio Quality

Track:

- return,
- volatility,
- Sharpe,
- Sortino,
- drawdown,
- exposure,
- concentration,
- realized vs expected risk.

## Layer 6: Execution Quality

Track:

- rejection rate,
- slippage,
- fill latency,
- partial fills,
- duplicated order attempts,
- reconciliation failures.

## Experiment Record

Each experiment should store:

```json
{
  "experiment_id": "exp_0042",
  "strategy_version": "momentum_v2",
  "feature_version": "features_v4",
  "model_version": "xgb_v3",
  "risk_version": "risk_v2",
  "data_range": [
    "2020-01-01",
    "2025-12-31"
  ],
  "code_commit": "abc123",
  "notes": "Added volatility filter"
}
```

## Failure Analysis

Every poor result should be classified where possible:

- data failure,
- feature failure,
- strategy failure,
- model failure,
- agent reasoning failure,
- risk failure,
- execution failure,
- market regime mismatch.

This is more useful than simply recording that a trade lost money.
