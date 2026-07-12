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
    return LayoutDetection(model_name=DETECT_MODEL)


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
