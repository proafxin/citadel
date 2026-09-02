import asyncio
import sys
from base64 import b64encode
from pathlib import Path

import httpx

from citadel.prompts import load_prompt

IMAGES_DIR = Path("/home/masterkenway/Projects/citadel/data/model_baselines/baseline_qwen3vl_8b_fp8/ocr_images")
BASE_URL = "http://localhost:8100/v1"


async def run_one(client: httpx.AsyncClient, path: Path, out_dir: Path) -> None:
    data = path.read_bytes()
    image_url = "data:image/png;base64," + b64encode(data).decode()
    payload = {
        "model": "lm",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": load_prompt("page_ocr")},
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": 8192,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    resp = await client.post("/chat/completions", json=payload)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    out_path = out_dir / f"{path.stem}.md"
    out_path.write_text(content)


async def main() -> None:
    out_dir = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path("/home/masterkenway/Projects/citadel/data/model_baselines/ocr_battery_output")
    )
    Path(out_dir).mkdir(exist_ok=True, parents=True)
    images = sorted(IMAGES_DIR.glob("*.png"))
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=httpx.Timeout(600.0)) as client:
        await asyncio.gather(*(run_one(client, path, out_dir) for path in images))


asyncio.run(main())
