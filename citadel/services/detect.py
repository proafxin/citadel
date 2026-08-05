import functools
import threading

import cv2
import numpy as np
from paddleocr import LayoutDetection
from pydantic import BaseModel

DETECT_MODEL = "PP-DocLayoutV3"

DETECT_THRESHOLD = {
    0: 0.5,
    1: 0.5,
    2: 0.5,
    3: 0.5,
    4: 0.5,
    5: 0.3,
    6: 0.3,
    7: 0.5,
    8: 0.5,
    9: 0.5,
    10: 0.5,
    11: 0.5,
    12: 0.5,
    13: 0.5,
    14: 0.5,
    15: 0.3,
    16: 0.5,
    17: 0.3,
    18: 0.5,
    19: 0.5,
    20: 0.45,
    21: 0.5,
    22: 0.3,
    23: 0.3,
    24: 0.5,
}

DETECT_NMS = True
DETECT_UNCLIP_RATIO = [1.0, 1.0]

DETECT_MERGE_MODE = {
    0: "union",
    1: "union",
    2: "union",
    3: "large",
    4: "union",
    5: "large",
    6: "large",
    7: "union",
    8: "union",
    9: "union",
    10: "union",
    11: "union",
    12: "union",
    13: "union",
    14: "union",
    15: "large",
    16: "union",
    17: "large",
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
                float(box["coordinate"][0]) / width,
                float(box["coordinate"][1]) / height,
                float(box["coordinate"][2]) / width,
                float(box["coordinate"][3]) / height,
            ],
            order=None if box.get("order") is None else int(box["order"]),
        )
        for box in result["boxes"]
    ]
