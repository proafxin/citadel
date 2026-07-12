import asyncio
import functools
import multiprocessing
import os
import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import xgboost as xgb

from citadel.schemas.table import TableStructure
from citadel.tabular.detect import featurize

MODEL = Path(__file__).resolve().parents[2] / "model.json"
HEADER_WORKERS = 1  # a header worker is an XGBoost predict, but it stands at ~250-650MB like every pool process here,
# and the work is trivial and rare: the tabular files in a library finish in the first seconds of a GPU-bound run.
# one worker is the whole stage's demand, and the other two were only ever standing memory


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


_NUMERIC = re.compile(r"^[-+]?\d[\d,]*\.?\d*([eE][-+]?\d+)?$")
_DATEISH = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _coarse_dtype(values: list[str]) -> str:
    present = [value.strip() for value in values if value and value.strip()]
    if not present:
        return "empty"
    if all(_NUMERIC.match(value) for value in present):
        return "number"
    if all(_DATEISH.match(value) for value in present):
        return "date"
    return "string"


def _cell(grid: list[list[str]], row: int, col: int) -> str:
    return grid[row][col] if 0 <= row < len(grid) and 0 <= col < len(grid[row]) else ""


def _profile(grid: list[list[str]], structure: TableStructure) -> tuple[int, tuple[str, ...]]:
    count = structure.col_end - structure.col_start + 1
    rows = range(structure.data_start, structure.data_end + 1)
    return count, tuple(
        _coarse_dtype([_cell(grid, row, structure.col_start + index) for row in rows]) for index in range(count)
    )


def merge_spurious_splits(grid: list[list[str]], structures: list[TableStructure]) -> list[TableStructure]:
    # a predicted header row only starts a NEW table when the schema actually changes: "a run of rows with consistent
    # columns is one table; a schema change starts a new one". splitting on EVERY predicted header hands the table
    # boundary to the header model — one false positive mid-sheet both fragments the table AND promotes a data row to
    # the next fragment's header. identical schema on both sides ⇒ that header was a data row: absorb it back as data.
    if len(structures) <= 1:
        return structures
    merged = [structures[0]]
    for candidate in structures[1:]:
        previous = merged[-1]
        if _profile(grid, previous) == _profile(grid, candidate):
            merged[-1] = previous.model_copy(update={"data_end": candidate.data_end})
        else:
            merged.append(candidate)
    return merged


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


def release_header_pool() -> None:
    # its workers are whole processes and it is idle between uploads; the next call forks it back off the forkserver
    if _pool.cache_info().currsize:
        _pool().shutdown(wait=True)
        _pool.cache_clear()


async def predict_pooled(rows: list[list[str]]) -> list[bool]:
    return await asyncio.get_running_loop().run_in_executor(_pool(), predict, rows)
