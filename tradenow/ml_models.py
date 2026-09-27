"""The registered Phase 7 models (ADR-012), fitted with scikit-learn.

This is the only module that uses floats or the model libraries. Samples come
in as Decimal and predictions go out as Decimal rounded to 10 places, so the
in/out decision and everything after it stay exact. Both models are
deterministic: neither uses randomness.
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.neighbors import KNeighborsRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .ml_dataset import Sample


RIDGE_ALPHA = 1.0
KNN_NEIGHBORS = 50
MODELS = {"ridge": f"Ridge(alpha={RIDGE_ALPHA})",
          "knn": f"KNeighborsRegressor(n_neighbors={KNN_NEIGHBORS})"}
PREDICTION_STEP = Decimal("1e-10")


def minimum_samples(model: str) -> int:
    """Fewest samples a model can be fitted on (nearest neighbours needs its k)."""
    return KNN_NEIGHBORS if model == "knn" else 1


def fit_predict(model: str, samples: Sequence[Sample],
                current: Mapping[str, tuple[Decimal, ...]]) -> dict[str, Decimal]:
    """Fit on the samples, standardized with their own mean and deviation only,
    and predict each current feature vector."""
    if model not in MODELS:
        raise ValueError(f"Unknown model {model!r}")
    if len(samples) < minimum_samples(model):
        raise ValueError(f"{model} needs at least {minimum_samples(model)} samples")
    if not current:
        return {}
    estimator = (Ridge(alpha=RIDGE_ALPHA) if model == "ridge"
                 else KNeighborsRegressor(n_neighbors=KNN_NEIGHBORS))
    pipeline = make_pipeline(StandardScaler(), estimator)
    features = np.array([[float(value) for value in sample.features] for sample in samples])
    labels = np.array([float(sample.label) for sample in samples])
    pipeline.fit(features, labels)
    symbols = list(current)
    predicted = pipeline.predict(np.array([[float(value) for value in current[symbol]]
                                           for symbol in symbols]))
    return {symbol: Decimal(float(value)).quantize(PREDICTION_STEP)
            for symbol, value in zip(symbols, predicted, strict=True)}
