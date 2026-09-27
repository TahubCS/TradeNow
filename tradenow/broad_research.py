"""The broad ETF universe study (ADR-013).

Ten registered candidates on 35 ETFs: ADR-011's rules with equal and capped
inverse-volatility sizing, relative strength with and without an absolute
filter, and ADR-012's two models pooled across all 35. It uses the same
evaluation, gate, and report as ADR-011. It needs the `ml` extra.
"""

from .ml_strategies import MlCandidate, Predictor
from .multi_evaluation import PortfolioCandidate
from .multi_research import CORE_CODE, Study
from .multi_strategies import RULES, MultiCandidate, RelativeStrength
from .universe import BROAD_UNIVERSE


BROAD_STRATEGY_FAMILY = "broad_adr013_candidates_v1"
# ADR-010 (12) + ADR-011 (6) + ADR-012 (4) + ADR-013 (10).
REGISTERED_TRIALS_TOTAL = 32

# The registered candidates (ADR-013), in registration order, which also breaks
# ties in selection. The models get their own predictors, so their caches never
# mix with ADR-012's. Changing this list needs a new ADR.
BROAD_CANDIDATES: tuple[PortfolioCandidate, ...] = (
    *(MultiCandidate(rule, "eq") for rule in RULES),
    *(MultiCandidate(rule, "iv10") for rule in RULES),
    RelativeStrength(absolute=False), RelativeStrength(absolute=True),
    MlCandidate("ridge", "eq", Predictor("ridge")), MlCandidate("knn", "eq", Predictor("knn")),
)

BROAD_STUDY = Study("broad_research", "Broad ETF universe research report", "ADR-013",
                    BROAD_STRATEGY_FAMILY, BROAD_CANDIDATES, REGISTERED_TRIALS_TOTAL,
                    CORE_CODE + ("ml_dataset.py", "ml_models.py", "ml_strategies.py",
                                 "broad_research.py", "data_check.py"),
                    BROAD_UNIVERSE)
