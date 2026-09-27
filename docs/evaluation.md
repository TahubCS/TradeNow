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

## Implemented GLD Historical Evaluation

`python -m tradenow gld` evaluates the latest private Tiingo import without
another API call. The GLD report preserves the original 60/20/20 chronological
development, validation, and holdout split. Only validation selects one of the
12 registered candidates (ADR-010); a nonpositive best score selects cash.

For the final holdout, compare the selected strategy with zero-interest cash
and GLD buy-and-hold at both the 50% position cap and 100% allocation. Report
return, calendar-time annualized return, maximum drawdown, and strategy gross
traded notional as a fraction of starting cash. Replay the chosen strategy on
the same holdout with larger per-share slippage without changing its selection.

As a separate stability diagnostic, run rolling 504-bar development, 126-bar
validation, and 126-bar test windows entirely before the final holdout. Test
blocks do not overlap; each begins flat. Record how often tests are positive
and beat the 50% buy-and-hold benchmark. SMA warmup occurs inside each test,
while buy-and-hold enters at its first open. These windows do not constitute new
independent evidence about the already viewed final holdout.

## Implemented Portfolio Metrics and Experiment Log

Every GLD report compares the selected strategy with 50% and 100% buy-and-hold
on the same holdout. Each gets return, annualized return and volatility,
Sharpe, Sortino, maximum drawdown, Calmar, and exposure. The strategy also
gets closed trades, hit rate, profit factor, and average holding period
(`tradenow/metrics.py`). Volatility-based ratios use daily close-to-close
equity, 252 days a year, and a zero risk-free rate. A ratio that cannot be
computed (no volatility, no losing trades) is reported as n/a, never as
infinite.

Each `python -m tradenow gld` run appends one record to
`data/private/experiments.jsonl`, following the experiment record above: the
candidates, the selected hypothesis, feature and strategy versions, the
configuration, data and code hashes, the Git commit, results, and the gate
verdict. The experiment ID is the report's run ID, a hash of its inputs, so
repeating an experiment adds nothing and every distinct trial is counted once.
Identical inputs produce byte-identical reports; a test enforces this.

## Implemented Paper Execution Evaluation

`python -m tradenow paper-report` covers Layer 6 for GLD paper orders without
contacting Alpaca. It compares each fill with the plan's reference close, the
session's actual open, and the backtest's simulated fill (open ± slippage per
share). It reports fill rate, rejection rate (rejected, or never received by
Alpaca), partial fills, buy limits that fell below the open, time from the
open to the fill, and total cost against the simulation. From the run log it
counts reconciliation failures and repeated submit attempts. Paper fills are
simulated by Alpaca, so these numbers test the plumbing and the backtest's fill
assumption, not live market impact.

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
