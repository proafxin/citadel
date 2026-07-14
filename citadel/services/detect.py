import functools
import threading

import cv2
import numpy as np
from paddleocr import LayoutDetection
from pydantic import BaseModel

# NOTE: paddle PRE-ALLOCATES ~92% of the card the moment it initialises, regardless of what else is already on it — we
# watched it do exactly that and then die trying to allocate 144MB. FLAGS_allocator_strategy=auto_growth and
# FLAGS_fraction_of_gpu_memory_to_use=0 are exported by scripts/run.sh, because they must be set before paddle is
# imported and this process shares the card with the recognition model, the SLM and the embedder.
DETECT_MODEL = "PP-DocLayoutV3"

# the detector's settings, from the reference pipeline (paddlex configs/pipelines/PaddleOCR-VL.yaml). they are NOT
# optional: constructed bare, the predictor falls back to the model's `draw_threshold` — a flat 0.5 on every class, with
# no nms and no box merging. that is a VISUALISATION default, not a recognition one, and it silently drops every text,
# formula and title region the model scores between 0.4 and 0.5 — content the reference pipeline keeps. lost content is
# the one failure nothing downstream can recover from, so the floor for a class is the reference's, never a round number
# the TEXT-bearing classes sit at 0.3, below the reference's 0.4. measured: on a form the detector DOES box the
# filled-in values — the address, the company name, the case number — and scores them 0.05-0.4, so the reference cutoff
# throws them away. a threshold sweep over the six worst-covered files took borang_13 from 0.796 of its text-layer
# characters to 0.889 and Borang 4 from 0.840 to 0.916, with NO file exceeding 1.0.
# 0.3 and not lower, deliberately: at 0.2 coverage keeps climbing but files start passing 1.0, which is not more text —
# it is the SAME text read twice by two overlapping boxes, and a duplicate is both a crop we pay for and a second copy
# in the tree. 0.3 is the last point that recovers content without inventing any.
DETECT_THRESHOLD = {
    0: 0.5,  # abstract
    1: 0.5,  # algorithm
    2: 0.5,  # aside_text
    3: 0.5,  # chart
    4: 0.5,  # content
    5: 0.3,  # display_formula
    6: 0.3,  # doc_title
    7: 0.5,  # figure_title
    8: 0.5,  # footer
    9: 0.5,  # footer
    10: 0.5,  # footnote
    11: 0.5,  # formula_number
    12: 0.5,  # header
    13: 0.5,  # header
    14: 0.5,  # image
    15: 0.3,  # inline_formula
    16: 0.5,  # number
    17: 0.3,  # paragraph_title
    18: 0.5,  # reference
    19: 0.5,  # reference_content
    20: 0.45,  # seal
    21: 0.5,  # table
    22: 0.3,  # text
    23: 0.3,  # text
    24: 0.5,  # vision_footnote
}

# a detector emits several overlapping candidates for the same region. without nms they ALL survive: every duplicate is
# a crop we pay the model for and a second copy of the same text in the tree
DETECT_NMS = True
DETECT_UNCLIP_RATIO = [1.0, 1.0]

# how two boxes covering the same region are reconciled. `large` keeps the enclosing box for the things that nest inside
# prose — a displayed formula, a title — so the region is read WHOLE, not clipped in half; `union` merges the pair
DETECT_MERGE_MODE = {
    0: "union",
    1: "union",
    2: "union",
    3: "large",  # chart
    4: "union",
    5: "large",  # display_formula
    6: "large",  # doc_title
    7: "union",
    8: "union",
    9: "union",
    10: "union",
    11: "union",
    12: "union",
    13: "union",
    14: "union",
    15: "large",  # inline_formula
    16: "union",
    17: "large",  # paragraph_title
    18: "union",
    19: "union",
    20: "union",
    21: "union",
    22: "union",
    23: "union",
    24: "union",
}


class DetBlock(BaseModel):
    label: str
    score: float
    bbox: list[float]
    order: int | None


@functools.lru_cache
def _thread_detector(_thread_id: int) -> LayoutDetection:
    # one detector per calling thread. the predictor is not documented as re-entrant, and a shared instance across
    # threads could race and garble the boxes — the same reason the scanned-page OCR engines were per-thread. keying on
    # thread id gives each worker its own; the pool is fixed and small, so this is a bounded few
    return LayoutDetection(
        model_name=DETECT_MODEL,
        threshold=DETECT_THRESHOLD,
        layout_nms=DETECT_NMS,
        layout_unclip_ratio=DETECT_UNCLIP_RATIO,
        layout_merge_bboxes_mode=DETECT_MERGE_MODE,
    )


def _detector() -> LayoutDetection:
    return _thread_detector(threading.get_ident())


def detect_layout(image: bytes) -> list[DetBlock]:
    # one RT-DETR forward pass: boxes, classes, and a reading order from the pointer network. no generation, so it
    # cannot loop, hallucinate, or return an empty page the way a layout call on a language model can.
    # cv2 decodes to BGR, which is what paddle's own file reader produces — hand it the same thing.
    page = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
    if page is None:
        raise ValueError("undecodable page image")
    height, width = page.shape[:2]
    result = _detector().predict(page)[0]
    return [
        DetBlock(
            label=str(box["label"]),
            score=float(box["score"]),
            bbox=[
                float(box["coordinate"][0]) / width,  # normalized, because every consumer downstream (cutting crops,
                float(box["coordinate"][1]) / height,  # the content tree, table anchors) is written against 0..1 boxes
                float(box["coordinate"][2]) / width,
                float(box["coordinate"][3]) / height,
            ],
            order=None if box.get("order") is None else int(box["order"]),
        )
        for box in result["boxes"]
    ]
