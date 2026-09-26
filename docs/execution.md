# Execution Architecture

## Goal

Provide a single controlled path from approved portfolio action to brokerage order.

## Components

### Order Manager

Responsible for:

- creating internal order IDs,
- translating target actions into broker orders,
- deduplicating requests,
- tracking order state,
- retrying safe failures,
- preventing repeated submissions.

### Broker Adapter

Defines a common interface across paper and live brokers.

```python
class BrokerAdapter:
    def submit_order(self, order):
        ...
    def cancel_order(self, order_id):
        ...
    def get_order(self, order_id):
        ...
    def get_positions(self):
        ...
```

## Environments

```text
Backtest
Paper Trading
Live Trading
```

The same higher-level order interface should work in each environment.

## Safety Requirements

- live trading disabled by default,
- environment clearly visible in configuration,
- secrets stored outside source control,
- idempotency on order submission,
- portfolio reconciliation after executions,
- emergency halt path,
- full order and fill logging.

## Order Lifecycle

```text
CREATED
  |
  v
VALIDATED
  |
  v
SUBMITTED
  |
  +--> REJECTED
  |
  +--> PARTIALLY_FILLED
  |
  v
FILLED
```

## Reconciliation

Broker state is the execution source of truth.

The system should periodically compare:

- internal positions,
- broker positions,
- cash,
- open orders,
- completed fills.

Any mismatch should block new live orders until reconciled.
