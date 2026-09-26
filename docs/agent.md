# Agent Design

## Role

The agent is responsible for interpreting structured market state and producing a structured trade proposal.

It is not responsible for:

- bypassing risk checks,
- sizing positions without constraints,
- calling the broker,
- modifying portfolio limits,
- inventing unavailable data,
- hiding uncertainty.

## Inputs

The agent may receive:

- market feature snapshot,
- deterministic strategy signals,
- ML model outputs,
- current positions,
- portfolio exposure,
- recent decisions,
- selected news or filing context,
- risk summary.

## Output Schema

```json
{
  "symbol": "MSFT",
  "action": "BUY",
  "time_horizon": "5-10d",
  "confidence": 0.74,
  "evidence": [
    "20-day momentum positive",
    "price above 50-day average",
    "model probability above threshold"
  ],
  "risks": [
    "elevated volatility",
    "earnings event approaching"
  ],
  "requested_exposure_pct": 2.0
}
```

## Required Behavior

The agent should:

- cite the structured evidence used,
- distinguish facts from inference,
- state uncertainty,
- prefer no-trade when evidence is weak,
- avoid generating unsupported narratives.

## Forbidden Behavior

The agent must not:

- fabricate prices,
- claim to have observed data not present in context,
- override rejected trades,
- increase risk because of subjective confidence,
- retry rejected trades using altered wording,
- place orders directly.

## Agent Evaluation

Agent quality should be measured separately from strategy profitability.

Track:

- proposal validity,
- schema compliance,
- evidence grounding,
- contradiction rate,
- risk-rule violation attempts,
- confidence calibration,
- no-trade discipline.

A profitable period does not prove that the agent is reasoning correctly.
