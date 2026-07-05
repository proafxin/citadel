import csv
import logging
from pathlib import Path

import numpy as np
import xgboost as xgb
from sklearn.metrics import classification_report, confusion_matrix

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
TRAIN = ROOT / "features_train.csv"
TEST = ROOT / "features_test.csv"


def _load(path: Path) -> tuple[np.ndarray, np.ndarray]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        next(reader)
        rows = [[float(value) for value in row] for row in reader if row]
    array = np.asarray(rows, dtype=np.float32)
    return array[:, :-1], array[:, -1].astype(np.int32)


def main() -> None:
    x_train, y_train = _load(TRAIN)
    x_test, y_test = _load(TEST)
    positives = int(y_train.sum())
    scale = (len(y_train) - positives) / positives
    model = xgb.XGBClassifier(
        n_estimators=400,
        max_depth=6,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=scale,
        tree_method="hist",
        device="cpu",
        eval_metric="aucpr",
    )
    model.fit(x_train, y_train)
    predictions = model.predict(x_test)
    logger.info("scale_pos_weight=%.2f", scale)
    logger.info("confusion (rows=true, cols=pred):\n%s", confusion_matrix(y_test, predictions))
    logger.info("\n%s", classification_report(y_test, predictions, target_names=["data", "header"], digits=4))


if __name__ == "__main__":
    main()
