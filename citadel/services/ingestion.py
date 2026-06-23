import asyncio
import base64
import logging
from functools import lru_cache
from pathlib import Path

import pymupdf
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from citadel.schemas.document import DocumentText, PageText
from config import get_settings

logger = logging.getLogger(__name__)

VLLM_MODEL = "Qwen/Qwen3.5-9B"
TEMPERATURE = 0.0
MAX_TOKENS = 4096
RENDER_DPI = 300
MAX_CONCURRENCY = 8

TRANSCRIBE_PROMPT = (
    "Transcribe all text in this image into clean Markdown, preserving the document's structure "
    "and reading order. Use headings for titles, bullet or numbered lists for lists, Markdown "
    "tables for tabular data, and bold or italics where the source emphasizes text. "
    "Output only the Markdown itself, with no commentary, explanation, or surrounding code fences."
)


@lru_cache
def get_client() -> AsyncOpenAI:
    return AsyncOpenAI(base_url=get_settings().vllm_base_url, api_key="EMPTY")


def _render(document: pymupdf.Document, dpi: int) -> list[bytes]:
    return [page.get_pixmap(dpi=dpi).tobytes("png") for page in document]


def render_pdf_pages(pdf_path: Path, dpi: int) -> list[bytes]:
    with pymupdf.open(pdf_path) as document:
        return _render(document, dpi)


def render_pdf_bytes(data: bytes, dpi: int) -> list[bytes]:
    with pymupdf.open(stream=data, filetype="pdf") as document:
        return _render(document, dpi)


def image_to_data_url(image_bytes: bytes) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def build_messages(data_url: str) -> list[ChatCompletionMessageParam]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": TRANSCRIBE_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]


async def transcribe_image(image_bytes: bytes, page_number: int, *, enable_thinking: bool = False) -> PageText:
    response = await get_client().chat.completions.create(
        model=VLLM_MODEL,
        messages=build_messages(image_to_data_url(image_bytes)),
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        extra_body={"chat_template_kwargs": {"enable_thinking": enable_thinking}},
    )
    choice = response.choices[0]
    truncated = choice.finish_reason == "length"
    if truncated:
        logger.warning(
            "Transcription for page %d truncated at max_tokens=%d; output is incomplete",
            page_number,
            MAX_TOKENS,
        )
    text = (choice.message.content or "").strip()
    return PageText(page_number=page_number, text=text, truncated=truncated)


async def _transcribe_with_limit(
    semaphore: asyncio.Semaphore, image_bytes: bytes, page_number: int, enable_thinking: bool
) -> PageText:
    async with semaphore:
        return await transcribe_image(image_bytes, page_number, enable_thinking=enable_thinking)


async def _transcribe_images(images: list[bytes], source: str, enable_thinking: bool) -> DocumentText:
    logger.info("Transcribing %d page(s) from %s", len(images), source)
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    tasks = [
        _transcribe_with_limit(semaphore, image_bytes, page_number, enable_thinking)
        for page_number, image_bytes in enumerate(images, start=1)
    ]
    pages = await asyncio.gather(*tasks)
    return DocumentText(source=source, pages=list(pages))


async def transcribe_pdf(pdf_path: Path, *, enable_thinking: bool = False) -> DocumentText:
    images = render_pdf_pages(pdf_path, dpi=RENDER_DPI)
    return await _transcribe_images(images, str(pdf_path), enable_thinking)


async def transcribe_pdf_bytes(data: bytes, source: str, *, enable_thinking: bool = False) -> DocumentText:
    images = render_pdf_bytes(data, dpi=RENDER_DPI)
    return await _transcribe_images(images, source, enable_thinking)
