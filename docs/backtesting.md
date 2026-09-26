# Backtesting

## Objective

The backtesting engine should answer whether a strategy behaves as expected under realistic historical conditions.

It should not be optimized merely to maximize historical returns.

## Required Inputs

- historical market data,
- feature history,
- strategy/model version,
- starting capital,
- transaction-cost assumptions,
- slippage assumptions,
- trading calendar,
- position-sizing rules,
- risk rules.

## Simulation Loop

```text
For each timestamp:
    load market state
    compute available features
    generate signals
    create proposal
    run risk checks
    simulate order
    update portfolio
    record state
```

## Realism Requirements

Include:

- bid/ask spread assumptions,
- slippage,
- transaction costs,
- market hours,
- position limits,
- delayed execution where appropriate,
- rejected or partially filled orders if modeled.

## Metrics

At minimum:

- total return,
- annualized return,
- volatility,
- Sharpe ratio,
- Sortino ratio,
- max drawdown,
- win rate,
- profit factor,
- turnover,
- average holding period,
- exposure,
- trade count.

## Validation

Use:

- train / validation / test periods,
- walk-forward testing,
- out-of-sample evaluation,
- sensitivity analysis,
- multiple market regimes.

## Anti-Overfitting Rules

Reject research that depends on:

- one unusually strong time period,
- excessive parameter tuning,
- many unreported failed variants,
- unrealistic costs,
- tiny trade counts,
- leakage.

Every experiment should record both successful and unsuccessful results.
