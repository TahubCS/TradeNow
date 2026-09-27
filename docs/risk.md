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

## Implemented GLD offline drawdown rule

The GLD simulator starts with a 10% threshold measured against the highest
**closing equity** seen in a run. On a closing breach it permanently blocks new
entries and requests a sale of any open shares at the next tradable bar's open.
This is a simulated next-open sale, not an intraday stop or a guaranteed 10%
loss cap. An overnight gap can make the loss larger. A zero-volume or stale-gap
bar blocks the sale and the simulator retries on a later bar. If the breach is
on the final bar, the position remains open and the report marks the exit pending.
The report records the trigger, simulated exit, and maximum closing-equity drawdown.

## Implemented GLD paper-trading controls

Paper trading applies the same 10% rule to Alpaca paper-account equity, measured
when each plan is made after the close. A breach is stored in the ledger, blocks
buys permanently for that paper run, and plans a market sale for the next open.
It is still not a guaranteed stop.

The kill switch (`paper-halt`) is fail-closed. It is written locally before any
network call, and an unreadable kill-switch file counts as engaged. It is
engaged automatically when reconciliation fails. Only `paper-resume` clears it,
and only when Alpaca and the ledger agree. Stale or disagreeing market data
blocks a plan without engaging the kill switch.

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
