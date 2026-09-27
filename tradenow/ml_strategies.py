"""The registered Phase 7 candidates (ADR-012) as portfolio allocators.

At each signal day a model is fitted on every sample whose label ended before
that day, pooled across the six assets, and predicts each asset's next month
from today's features. An asset is in when its prediction is above zero and
sized like ADR-011 (eq or iv35). Until labels from 24 month-ends exist (and 50
samples for nearest neighbours), every asset is out.

A prediction depends only on the views it is given, so the fitted results are
cached per signal date. The cache belongs to one set of feature rows: views
over different rows (another universe, or a truncated history) clear it.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .equity_types import FeatureRow
from .ml_dataset import MIN_LABEL_MONTHS, feature_vector, label_months, training_samples
from .ml_models import MODELS, fit_predict, minimum_samples
from .multi_strategies import SIZINGS, sized_weights
from .strategies import History


class Predictor:
    """One model's predictions per signal day, cached for one set of rows."""

    def __init__(self, model: str):
        if model not in MODELS:
            raise ValueError(f"Unknown model {model!r}")
        self.model = model
        self._anchor: tuple[FeatureRow, ...] = ()
        self._cache: dict[tuple[tuple[str, ...], date], dict[str, Decimal]] = {}

    def __call__(self, views: Mapping[str, History]) -> dict[str, Decimal]:
        """Predictions for the assets with complete features today; empty while
        the model cannot be fitted yet."""
        # First rows identify the underlying data; holding them keeps the check sound.
        anchor = tuple(view[0] for view in views.values())
        if len(anchor) != len(self._anchor) or any(
                new is not old for new, old in zip(anchor, self._anchor, strict=True)):
            self._anchor, self._cache = anchor, {}
        key = (tuple(views), next(iter(views.values())).today.date)
        if key not in self._cache:
            self._cache[key] = self._predict(views)
        return self._cache[key]

    def _predict(self, views: Mapping[str, History]) -> dict[str, Decimal]:
        samples = training_samples(views)
        if (label_months(samples) < MIN_LABEL_MONTHS
                or len(samples) < minimum_samples(self.model)):
            return {}
        current = {}
        for symbol, view in views.items():
            vector = feature_vector(view.today)
            if vector is not None:
                current[symbol] = vector
        return fit_predict(self.model, samples, current)


@dataclass(frozen=True)
class MlCandidate:
    model: str
    sizing: str
    predictor: Predictor = field(compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.model != self.predictor.model or self.sizing not in SIZINGS:
            raise ValueError(f"Unregistered candidate {self.model}_{self.sizing}")

    @property
    def name(self) -> str:
        return f"{self.model}_{self.sizing}"

    @property
    def parameters(self) -> dict[str, str]:
        return {"model": MODELS[self.model], "sizing": self.sizing,
                "in_when": "prediction > 0"}

    def __call__(self, views: Mapping[str, History]) -> dict[str, Decimal]:
        """Target weights after today's close (an Allocator for portfolio.py)."""
        predictions = self.predictor(views)
        return sized_weights(self.sizing, {symbol: view.today for symbol, view in views.items()},
                             {symbol for symbol, value in predictions.items() if value > 0})


# One predictor per model, shared by its two sizings so each fit runs once.
_RIDGE, _KNN = Predictor("ridge"), Predictor("knn")

# The registered candidates (ADR-012), in registration order, which also breaks
# ties in selection. Changing this list needs a new ADR.
ML_CANDIDATES: tuple[MlCandidate, ...] = (
    MlCandidate("ridge", "eq", _RIDGE), MlCandidate("ridge", "iv35", _RIDGE),
    MlCandidate("knn", "eq", _KNN), MlCandidate("knn", "iv35", _KNN),
)
