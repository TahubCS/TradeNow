"""The Phase 7 machine-learning study (ADR-012).

It runs through the same evaluation, gate, and report as ADR-011, with the
four registered model candidates. Import it only where the `ml` extra
(scikit-learn) is installed; other commands never load it.
"""

from .ml_strategies import ML_CANDIDATES
from .multi_research import CORE_CODE, Study


ML_STRATEGY_FAMILY = "ml_adr012_candidates_v1"
# ADR-010 (12) + ADR-011 (6) + ADR-012 (4).
REGISTERED_TRIALS_TOTAL = 22

ML_STUDY = Study("ml_research", "Machine-learning research report", "ADR-012",
                 ML_STRATEGY_FAMILY, ML_CANDIDATES, REGISTERED_TRIALS_TOTAL,
                 CORE_CODE + ("ml_dataset.py", "ml_models.py", "ml_strategies.py",
                              "ml_research.py"))
