# Strategy Design

## Purpose

Strategies convert market features into measurable signals. They should be testable independently from the agent layer.

## Development Order

1. deterministic baseline strategies,
2. statistical / ML strategies,
3. agent-assisted decision logic.

Do not reverse this order.

## Baseline Strategies

Initial candidates:

- trend following,
- time-series momentum,
- cross-sectional momentum,
- mean reversion,
- breakout,
- volatility filter.

These baselines are useful even if they are not profitable because they verify that the research and backtesting stack behaves correctly.

No strategy trades real money unless it passes the live-trading gate in
ADR-008: it must beat plain buy-and-hold out of sample, after costs, over
enough trades, historically and in a year of forward paper trading. Until one
does, a low-cost index fund or simply holding GLD is expected to do better.

## Strategy Interface

```python
class Strategy:
    def generate_signal(self, snapshot, portfolio):
        ...
```

Example signal:

```json
{
  "symbol": "AAPL",
  "direction": "LONG",
  "strength": 0.64,
  "horizon": "5d",
  "source": "momentum_v1"
}
```

## ML Strategy

Start with tree-based models for tabular market features.

Candidate models:

- logistic regression baseline,
- random forest,
- XGBoost,
- LightGBM.

Example prediction target:

```text
P(5-day forward return > 0)
```

Avoid using future information in features.

## Leakage Controls

Explicitly guard against:

- forward-filled future data,
- using revised data not available at the original timestamp,
- normalization using future samples,
- training/test overlap,
- survivorship bias,
- look-ahead bias.

## Model Output

ML models should emit a probability or expected return, not a final order.

Example:

```json
{
  "symbol": "MSFT",
  "model": "xgb_5d_v3",
  "prob_up": 0.67,
  "expected_return": 0.018,
  "timestamp": "2026-09-25T20:00:00Z"
}
```

That output becomes one input to the proposal process.

## Implemented Strategy Interface

`tradenow/strategies.py` defines the interface every GLD strategy uses. A
strategy reads features known at a close through `History`, a read-only view
that raises an error on any later day, and returns a `Decision`: a target
from 0 (cash) to 1 (the full position the risk limits allow), plus the
evidence it used. That evidence is logged with every simulated signal and
saved in every paper plan.

One strategy object drives the backtest (`simulate_equity`), the rolling
evaluation, and `paper-plan`, and `tradenow/selection.py` holds the single
validation-only selection rule, so the rule tested is the rule traded.
Fractional targets set the entry size; positions are not rebalanced while
held.

The registered GLD candidates are `GLD_CANDIDATES`. Moving the original SMA
rule onto this interface changed no result: a regression test holds the
SHA-256 of reports and simulations produced before the change.
