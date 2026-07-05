import functools
from pathlib import Path

import numpy as np
import xgboost as xgb

from citadel.tabular.detect import featurize

MODEL = Path(__file__).resolve().parents[2] / "model.json"


@functools.lru_cache(maxsize=1)
def _model() -> xgb.XGBClassifier:
    model = xgb.XGBClassifier()
    model.load_model(MODEL)
    return model


def predict(rows: list[list[str]]) -> list[bool]:
    if not rows:
        return []
    vectors = np.asarray(featurize(rows), dtype=np.float32)
    return [bool(value) for value in _model().predict(vectors)]
