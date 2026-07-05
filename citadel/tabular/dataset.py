import csv
import json
import logging
import random
from pathlib import Path

from citadel.tabular.detect import FEATURE_NAMES, featurize

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "dataset.csv"
TRAIN = ROOT / "features_train.csv"
TEST = ROOT / "features_test.csv"
TRAIN_FRACTION = 0.8
SEED = 0


def _records() -> list[tuple[list[str], int, int]]:
    with DATASET.open(newline="", encoding="utf-8") as dataset:
        reader = csv.reader(dataset)
        next(reader)
        return [(json.loads(row[0]), int(row[1]), int(row[2])) for row in reader if row]


def _split() -> tuple[list[tuple[list[float], int]], list[tuple[list[float], int]]]:
    records = _records()
    rng = random.Random(SEED)
    train: list[tuple[list[float], int]] = []
    test: list[tuple[list[float], int]] = []
    position = 0
    while position < len(records):
        size = records[position][2]
        block = records[position : position + size]
        position += size
        rows = [cells for cells, _, _ in block]
        samples = [(vector, label) for vector, (_, label, _) in zip(featurize(rows), block, strict=True)]
        (train if rng.random() < TRAIN_FRACTION else test).extend(samples)
    rng.shuffle(train)
    rng.shuffle(test)
    return train, test


def _write(path: Path, samples: list[tuple[list[float], int]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([*FEATURE_NAMES, "label"])
        for vector, label in samples:
            writer.writerow([*vector, label])


def main() -> None:
    train, test = _split()
    _write(TRAIN, train)
    _write(TEST, test)
    logger.info(
        "train %d (%d headers), test %d (%d headers)",
        len(train),
        sum(label for _, label in train),
        len(test),
        sum(label for _, label in test),
    )


if __name__ == "__main__":
    main()
