"""Choose one registered GLD candidate using validation data only.

The rule is unchanged from the original SMA research: the highest validation
return minus maximum drawdown wins, and a best score that is not positive
selects cash. Reports, rolling windows, and paper-plan all call this one
function, so they cannot drift apart.
"""

from dataclasses import dataclass, replace
from decimal import Decimal

from .equity import EquityConfig, simulate_equity
from .equity_types import EquityBar
from .strategies import GLD_CANDIDATES, Candidate


@dataclass(frozen=True)
class Selection:
    selected: Candidate | None  # None: no candidate earned a positive score (cash)
    best: Candidate
    best_score: Decimal
    hypotheses: list[dict]


def validation_score(result: dict) -> Decimal:
    return Decimal(result["total_return_pct"]) - Decimal(result["max_drawdown_pct"])


def select_candidate(development: list[EquityBar], validation: list[EquityBar],
                     config: EquityConfig,
                     candidates: tuple[Candidate, ...] = GLD_CANDIDATES) -> Selection:
    if not candidates:
        raise ValueError("At least one candidate is required")
    hypotheses = []
    best: Candidate | None = None
    best_score: Decimal | None = None
    for candidate in candidates:
        development_result = simulate_equity(development, config, candidate.strategy)
        validation_result = simulate_equity(validation, config, candidate.strategy)
        score = validation_score(validation_result)
        hypotheses.append({"name": candidate.name, "parameters": candidate.parameters,
                           "development": development_result,
                           "validation": validation_result, "validation_score": str(score)})
        if best_score is None or score > best_score:
            best, best_score = candidate, score
    assert best is not None and best_score is not None
    return Selection(best if best_score > 0 else None, best, best_score, hypotheses)


def chronological_split(bars: list[EquityBar]) -> dict[str, list[EquityBar]]:
    """The fixed 60/20/20 development, validation, and holdout periods."""
    development_end, validation_end = len(bars) * 3 // 5, len(bars) * 4 // 5
    return {"development": bars[:development_end],
            "validation": bars[development_end:validation_end],
            "holdout": bars[validation_end:]}


def run_selected(bars: list[EquityBar], config: EquityConfig, selection: Selection) -> dict:
    """Test the selection on new bars; with no selection the best candidate stays in cash."""
    return simulate_equity(bars, replace(config, enable_entries=selection.selected is not None),
                           selection.best.strategy)
