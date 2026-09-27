"""Choose one registered GLD candidate using validation data only.

The rule is unchanged from the original SMA research: the highest validation
return minus maximum drawdown wins, and a best score that is not positive
selects cash. Reports, rolling windows, and paper-plan all call this one
function, so they cannot drift apart.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from .equity import EquityConfig, simulate_equity
from .equity_types import EquityBar, FeatureRow
from .features import compute_features
from .strategies import GLD_CANDIDATES, Candidate


# The registered research setting (ADR-010): a fully invested position when a
# strategy is in, so timing is compared fairly with 100% buy-and-hold. The
# drawdown halt, slippage, and commission stay at their defaults.
RESEARCH_CONFIG = replace(EquityConfig(), max_position_fraction=Decimal("1.00"))


@dataclass(frozen=True)
class Selection:
    selected: Candidate | None  # None: no candidate earned a positive score (cash)
    best: Candidate
    best_score: Decimal
    hypotheses: list[dict]


def validation_score(result: dict) -> Decimal:
    return Decimal(result["total_return_pct"]) - Decimal(result["max_drawdown_pct"])


def history_rows(bars: list[EquityBar],
                 candidates: tuple[Candidate, ...]) -> list[FeatureRow] | None:
    """Features from the whole history, computed once. Each row still uses only
    earlier bars, so windows can use long lookbacks without seeing the future."""
    if any(item.strategy.needs_features for item in candidates):
        return compute_features(bars)
    return None


def _slice(rows: Sequence[FeatureRow] | None, start: int, end: int) -> list[FeatureRow] | None:
    return None if rows is None else list(rows[start:end])


def select_candidate(development: list[EquityBar], validation: list[EquityBar],
                     config: EquityConfig,
                     candidates: tuple[Candidate, ...] = GLD_CANDIDATES,
                     development_rows: Sequence[FeatureRow] | None = None,
                     validation_rows: Sequence[FeatureRow] | None = None) -> Selection:
    if not candidates:
        raise ValueError("At least one candidate is required")
    hypotheses = []
    best: Candidate | None = None
    best_score: Decimal | None = None
    for candidate in candidates:
        development_result = simulate_equity(development, config, candidate.strategy,
                                             development_rows)
        validation_result = simulate_equity(validation, config, candidate.strategy,
                                            validation_rows)
        score = validation_score(validation_result)
        hypotheses.append({"name": candidate.name, "parameters": candidate.parameters,
                           "development": development_result,
                           "validation": validation_result, "validation_score": str(score)})
        if best_score is None or score > best_score:
            best, best_score = candidate, score
    assert best is not None and best_score is not None
    return Selection(best if best_score > 0 else None, best, best_score, hypotheses)


def split_points(count: int) -> tuple[int, int]:
    """Ends of the fixed 60/20/20 development and validation periods."""
    return count * 3 // 5, count * 4 // 5


def chronological_split(bars: list[EquityBar]) -> dict[str, list[EquityBar]]:
    development_end, validation_end = split_points(len(bars))
    return {"development": bars[:development_end],
            "validation": bars[development_end:validation_end],
            "holdout": bars[validation_end:]}


def split_rows(rows: Sequence[FeatureRow] | None,
               count: int) -> dict[str, list[FeatureRow] | None]:
    development_end, validation_end = split_points(count)
    return {"development": _slice(rows, 0, development_end),
            "validation": _slice(rows, development_end, validation_end),
            "holdout": _slice(rows, validation_end, count)}


def run_selected(bars: list[EquityBar], config: EquityConfig, selection: Selection,
                 rows: Sequence[FeatureRow] | None = None) -> dict:
    """Test the selection on new bars; with no selection the best candidate stays in cash."""
    return simulate_equity(bars, replace(config, enable_entries=selection.selected is not None),
                           selection.best.strategy, rows)
