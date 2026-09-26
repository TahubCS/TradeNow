# Risk Management

## Purpose

The risk system protects the portfolio from strategy errors, model errors, agent errors, bad data, and execution mistakes.

Risk logic must remain deterministic.

## Example Limits

Initial paper-trading limits may include:

- max position size: 5% of portfolio,
- max sector exposure: 25%,
- max open positions: configurable,
- max daily portfolio loss: 2%,
- max portfolio drawdown: 10%,
- minimum liquidity threshold,
- maximum order size relative to average volume,
- no new entries around configurable event windows.

These values are examples and should be tuned for the simulation environment.

## Validation Flow

```text
Trade Proposal
      |
      v
Schema Validation
      |
      v
Instrument Validation
      |
      v
Position Limit
      |
      v
Portfolio Exposure
      |
      v
Loss / Drawdown Limits
      |
      v
Liquidity / Event Checks
      |
      v
APPROVE or REJECT
```

## Risk Decision

```json
{
  "proposal_id": "tp_00125",
  "approved": false,
  "reason_codes": [
    "SECTOR_EXPOSURE_LIMIT"
  ],
  "current_sector_exposure_pct": 24.7,
  "requested_sector_exposure_pct": 27.1
}
```

## Kill Switches

The system should be able to halt new orders when:

- data becomes stale,
- broker connectivity becomes unstable,
- PnL exceeds loss thresholds,
- unexpected order duplication occurs,
- portfolio state cannot be reconciled,
- risk service is unavailable.

Default behavior should be fail-closed, not fail-open.

## Position Sizing

The sizing system should use explicit rules.

Example:

```text
target risk per trade = 0.5% of portfolio
```

Position size can then be derived from volatility or stop distance.

The agent's confidence value should not directly determine capital allocation.
