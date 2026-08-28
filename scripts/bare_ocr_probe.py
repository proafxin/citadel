import asyncio
import logging
import pathlib
import sys
from base64 import b64encode

from citadel.llm import _post_qwen
from config import QWEN_MODEL

logger = logging.getLogger("bareocr")

IMAGE_DIR = pathlib.Path("data/model_baselines/baseline_qwen3vl_8b_fp8/ocr_images")
SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_TOKENS = 3072
CONCURRENCY = 4


async def _probe(path: pathlib.Path, gate: asyncio.Semaphore) -> tuple[str, str]:
    image_url = "data:image/png;base64," + b64encode(path.read_bytes()).decode()
    payload = {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": image_url}}]}],
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    async with gate:
        return path.name, (await _post_qwen(payload)).strip()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    targets = tuple(sys.argv[1:])
    paths = sorted(p for p in IMAGE_DIR.iterdir() if p.suffix.lower() in SUFFIXES)
    if targets:
        paths = [p for p in paths if p.name in targets]
    logger.info("probing %d images", len(paths))

    gate = asyncio.Semaphore(CONCURRENCY)
    for name, answer in await asyncio.gather(*(_probe(path, gate) for path in paths)):
        logger.info("=== %s ===\n%s\n", name, answer)


if __name__ == "__main__":
    asyncio.run(main())
