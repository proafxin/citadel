import asyncio
import functools
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import xgboost as xgb

from citadel.schemas.table import TableStructure
from citadel.tabular.detect import featurize

MODEL = Path(__file__).resolve().parents[2] / "model.json"
HEADER_WORKERS = 4


def structure_from_mask(grid: list[list[str]], mask: list[bool]) -> list[TableStructure]:
    # the header mask IS the structure: consecutive header rows form a header block; each block owns the data rows
    # beneath it up to the next block (or grid end). a repeated/stacked header simply starts a new region. rows before
    # the first block are preamble and dropped; a mask with no header rows yields one headerless region over all rows
    height = len(grid)
    if height == 0:
        return []
    width = len(grid[0])
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    for index in range(height):
        if index < len(mask) and mask[index]:
            start = index if start is None else start
        elif start is not None:
            blocks.append((start, index - 1))
            start = None
    if start is not None:
        blocks.append((start, height - 1))
    if not blocks:
        return [TableStructure(col_start=0, col_end=width - 1, header_rows=[], data_start=0, data_end=height - 1)]
    structures: list[TableStructure] = []
    for position, (head_start, head_end) in enumerate(blocks):
        data_start = head_end + 1
        data_end = blocks[position + 1][0] - 1 if position + 1 < len(blocks) else height - 1
        if data_start > data_end:
            continue
        structures.append(
            TableStructure(
                col_start=0,
                col_end=width - 1,
                header_rows=list(range(head_start, head_end + 1)),
                data_start=data_start,
                data_end=data_end,
            )
        )
    return structures


@functools.lru_cache(maxsize=1)
def _model() -> xgb.XGBClassifier:
    model = xgb.XGBClassifier()
    model.load_model(MODEL)
    model.set_params(n_jobs=1)
    return model


def predict(rows: list[list[str]]) -> list[bool]:
    if not rows:
        return []
    vectors = np.asarray(featurize(rows), dtype=np.float32)
    return [bool(value) for value in _model().predict(vectors)]


def _cpu_only() -> None:
    os.environ["CUDA_VISIBLE_DEVICES"] = "-1"


@functools.lru_cache(maxsize=1)
def _pool() -> ProcessPoolExecutor:
    # forkserver preloading only this module → xgboost/numpy workers, not the whole worker.py stack that spawn drags in
    ctx = multiprocessing.get_context("forkserver")
    ctx.set_forkserver_preload(["citadel.tabular.infer"])
    return ProcessPoolExecutor(max_workers=HEADER_WORKERS, mp_context=ctx, initializer=_cpu_only)


async def predict_pooled(rows: list[list[str]]) -> list[bool]:
    return await asyncio.get_running_loop().run_in_executor(_pool(), predict, rows)
