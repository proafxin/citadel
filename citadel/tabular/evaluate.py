import csv
import json
import logging
import random
from collections import Counter
from pathlib import Path

import numpy as np
import xgboost as xgb

from citadel.tabular.dataset import DATASET, SEED, TRAIN_FRACTION
from citadel.tabular.detect import featurize

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL = Path(__file__).resolve().parents[2] / "model.json"


def _blocks() -> list[tuple[list[list[str]], list[int]]]:
    with DATASET.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        next(reader)
        records = [(json.loads(row[0]), int(row[1]), int(row[2])) for row in reader if row]
    blocks: list[tuple[list[list[str]], list[int]]] = []
    position = 0
    while position < len(records):
        size = records[position][2]
        chunk = records[position : position + size]
        position += size
        blocks.append(([cells for cells, _, _ in chunk], [label for _, label, _ in chunk]))
    return blocks


def _category(headers: list[int]) -> str:
    if not headers:
        return "none"
    if headers == [0]:
        return "row0"
    if len(headers) == 1:
        return "offset"
    return "multirow"


def main() -> None:
    model = xgb.XGBClassifier()
    model.load_model(MODEL)
    rng = random.Random(SEED)
    files: Counter[str] = Counter()
    exact: Counter[str] = Counter()
    header_total: Counter[str] = Counter()
    header_hit: Counter[str] = Counter()
    tp = fp = fn = 0
    for rows, labels in _blocks():
        is_test = rng.random() >= TRAIN_FRACTION
        if not is_test:
            continue
        predictions = model.predict(np.asarray(featurize(rows), dtype=np.float32))
        true = {index for index, value in enumerate(labels) if value == 1}
        pred = {index for index, value in enumerate(predictions) if value == 1}
        category = _category(sorted(true))
        files[category] += 1
        exact[category] += int(pred == true)
        header_total[category] += len(true)
        header_hit[category] += len(true & pred)
        tp += len(true & pred)
        fp += len(pred - true)
        fn += len(true - pred)
    for category in ("row0", "offset", "multirow", "none"):
        count = files[category]
        if not count:
            continue
        recall = header_hit[category] / header_total[category] if header_total[category] else 1.0
        logger.info(
            "%-9s files=%3d  exact-header-match=%.3f  header-row-recall=%.3f",
            category,
            count,
            exact[category] / count,
            recall,
        )
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    logger.info("row-level header  P=%.4f R=%.4f F1=%.4f", precision, recall, f1)
    logger.info("per-file exact-header-match overall=%.4f", sum(exact.values()) / sum(files.values()))


if __name__ == "__main__":
    main()
