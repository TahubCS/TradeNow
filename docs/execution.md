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

## Implemented: GLD on Alpaca paper

`tradenow/alpaca_paper.py` is the broker adapter. It accepts only the paper
endpoint, blocks redirects, and validates every response into typed records
before the rest of the system sees it. `tradenow/paper_rules.py` holds the pure
rules (signal, data cross-check, reconciliation, drawdown, sizing), and
`tradenow/paper_trading.py` orchestrates them:

```text
paper-plan: kill switch? -> paper account? -> market closed? -> reconcile
            -> select strategy (validation only) -> Tiingo signal
            -> Alpaca SIP cross-check -> drawdown -> saved plan (no order)
paper-submit --approve ID: kill switch? -> plan unchanged and not expired
            -> reconcile -> ledger write -> POST with client order ID
paper-report: ledger + plans + Tiingo history + run log -> execution quality
```

Each ledger order stores the plan's reference close and the time it was sent.
Reconciliation copies Alpaca's `submitted_at` and `filled_at` into the order.
`tradenow/execution_quality.py` turns these into slippage (against the plan's
close, the session open, and the simulator's fill), fill latency, and fill and
rejection rates. Every command is also recorded once in
`data/private/logs/runs.jsonl`, including refusals with their `PaperBlocked`
code.

The local ledger (`data/private/alpaca/ledger.json`) is the internal position
record: the sum of filled quantities of the orders this system sent. An order is
written with status `submitting` before the request. If the request fails, the
order is looked up by client order ID. A later run marks it `not_found` only
after Alpaca confirms that it does not exist, so an ambiguous timeout can never
lead to a second submission.
