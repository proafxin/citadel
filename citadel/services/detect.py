import functools

import cv2
import numpy as np
from paddleocr import LayoutDetection
from pydantic import BaseModel

DETECT_MODEL = "PP-DocLayoutV3"


class DetBlock(BaseModel):
    label: str
    score: float
    bbox: list[float]
    order: int | None


@functools.lru_cache
def _detector() -> LayoutDetection:
    return LayoutDetection(model_name=DETECT_MODEL)


def detect_layout(image: bytes) -> list[DetBlock]:
    # one RT-DETR forward pass: boxes, classes, and a reading order from the pointer network. no generation, so it
    # cannot loop, hallucinate, or return an empty page the way a layout call on a language model can.
    # cv2 decodes to BGR, which is what paddle's own file reader produces — hand it the same thing.
    page = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
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
