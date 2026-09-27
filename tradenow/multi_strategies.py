"""The registered multi-asset candidates (ADR-011) as portfolio allocators.

At each signal day's close a candidate decides, per asset, whether it is in
(its rule holds) and how much of equity it gets (its sizing). Assets that are
out, or still warming up, hold cash. A candidate reads each asset only
through a History view that ends at that close, so it cannot see the future.

Rules: mom (12-1 month momentum above 0), trend (close above its 200-day
average), both (both hold). Sizing: eq (1/6 of equity each) or iv35 (inverse
60-day volatility, capped at 35% per asset; ADR-011 clarification 13).

Nothing here performs I/O, and all arithmetic uses Decimal.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from .equity_types import FeatureRow
from .strategies import History


RULES = ("mom", "trend", "both")
SIZINGS = ("eq", "iv35")
IV_CAP = Decimal("0.35")
# Weights are rounded down so they never add up to more than 100%.
WEIGHT_STEP = Decimal("1e-10")


def _floor(weight: Decimal) -> Decimal:
    return weight.quantize(WEIGHT_STEP, rounding=ROUND_DOWN)


def rule_holds(rule: str, row: FeatureRow) -> bool:
    """Whether an asset is in; a value still warming up means out."""
    momentum, trend = row["mom_12_1"], row["dist_sma_200"]
    mom_in = momentum is not None and momentum > 0
    trend_in = trend is not None and trend > 0
    if rule == "mom":
        return mom_in
    if rule == "trend":
        return trend_in
    if rule == "both":
        return mom_in and trend_in
    raise ValueError(f"Unknown rule {rule!r}")


def capped_inverse_volatility(volatility: Mapping[str, Decimal | None],
                              cap: Decimal = IV_CAP) -> dict[str, Decimal]:
    """Inverse volatility over the assets that have a positive value, normalized,
    then capped: excess above the cap is shared among the uncapped assets in
    proportion to their weights, until none exceeds it. Excess that no asset
    can take (too few assets) stays in cash."""
    inverse = {symbol: 1 / value for symbol, value in volatility.items()
               if value is not None and value > 0}
    if not inverse:
        return {}
    total = sum(inverse.values(), Decimal(0))
    base = {symbol: value / total for symbol, value in inverse.items()}
    weights = dict(base)
    capped: set[str] = set()
    while True:
        over = [symbol for symbol in weights if symbol not in capped and weights[symbol] > cap]
        if not over:
            break
        capped.update(over)
        free = [symbol for symbol in weights if symbol not in capped]
        free_base = sum((base[symbol] for symbol in free), Decimal(0))
        remaining = 1 - cap * len(capped)
        for symbol in capped:
            weights[symbol] = cap
        for symbol in free:
            weights[symbol] = remaining * base[symbol] / free_base
    return {symbol: _floor(weight) for symbol, weight in weights.items()}


@dataclass(frozen=True)
class MultiCandidate:
    rule: str
    sizing: str

    def __post_init__(self) -> None:
        if self.rule not in RULES or self.sizing not in SIZINGS:
            raise ValueError(f"Unregistered candidate {self.rule}_{self.sizing}")

    @property
    def name(self) -> str:
        return f"{self.rule}_{self.sizing}"

    @property
    def parameters(self) -> dict[str, str]:
        return {"rule": self.rule, "sizing": self.sizing}

    def __call__(self, views: Mapping[str, History]) -> dict[str, Decimal]:
        """Target weights after today's close (an Allocator for portfolio.py)."""
        rows = {symbol: view.today for symbol, view in views.items()}
        return sized_weights(self.sizing, rows,
                             {symbol for symbol, row in rows.items()
                              if rule_holds(self.rule, row)})


def sized_weights(sizing: str, rows: Mapping[str, FeatureRow],
                  in_symbols: set[str]) -> dict[str, Decimal]:
    """Weights for the assets that are in; the others keep their share in cash.
    eq gives each asset 1/n of equity; iv35 caps inverse volatility over every
    asset with a value, before out assets are dropped (clarification 13)."""
    if sizing == "eq":
        weights = dict.fromkeys(rows, _floor(Decimal(1) / len(rows)))
    elif sizing == "iv35":
        weights = capped_inverse_volatility({symbol: row["vol_60"]
                                             for symbol, row in rows.items()})
    else:
        raise ValueError(f"Unknown sizing {sizing!r}")
    return {symbol: weight for symbol, weight in weights.items() if symbol in in_symbols}


# The registered candidates (ADR-011), in registration order, which also
# breaks ties in selection. Changing this list needs a new ADR.
MULTI_CANDIDATES: tuple[MultiCandidate, ...] = tuple(
    MultiCandidate(rule, sizing) for sizing in SIZINGS for rule in RULES)
