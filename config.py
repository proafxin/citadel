import logging
import os
from functools import lru_cache
from pathlib import Path

from loguru import logger as loguru_logger
from pydantic_settings import BaseSettings, SettingsConfigDict

CPU_THIRD = max((os.cpu_count() or 3) // 3, 1)  # per-worker CPU stages: a third of the cores, at least 1


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    loguru_logger.disable("mineru_vl_utils")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CITADEL_", env_file=".env", extra="ignore")

    mineru_host: str = "localhost"
    mineru_port: int = 8099
    mineru_max_connections: int = (
        256  # hard cap on the shared httpx pool → bounds VLM sockets (match server max-num-seqs)
    )

    qwen_host: str = "localhost"
    qwen_port: int = 11434  # ollama OpenAI-compatible endpoint (vLLM hung on Blackwell sm_120 FlashInfer kernels)
    qwen_model: str = "qwen3:8b"  # exact ollama tag (`ollama list`)

    redis_url: str = "redis://localhost:6379/0"

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str
    postgres_db: str = "citadel"

    tree_store_dir: Path = Path("data/trees")

    render_dpi: int = 150  # validated equal to 200 (the VLM resizes internally) and ~26% faster
    digital_render_dpi: int = 110  # office→pdf ONLY (provably born-digital): image is layout-only, text from the PDF layer → render small. validate layout still holds; regular pdf stays at render_dpi
    normalize_concurrency: int = CPU_THIRD  # one isolated libreoffice profile per worker (per-job soffice)
    paginate_concurrency: int = CPU_THIRD  # pdfium process-pool workers, one dedicated pdfium per process
    ocr_concurrency: int = 128  # pages in flight; sockets are capped by mineru_max_connections, so keep this high to feed the server (born-digital makes few requests/page → needs many concurrent pages)
    merge_concurrency: int = 4  # light assembly
    slm_concurrency: int = 2  # concurrent ollama SLM calls (heading-leveling); keep <= OLLAMA_NUM_PARALLEL, low so it doesn't starve MinerU OCR
    worker_id: str = "0"  # stable per-replica id → deterministic Redis consumer names; set distinctly per replica
    rapidocr_concurrency: int = CPU_THIRD  # scanned-page gap-OCR threads (CPU); bounds RapidOCR so it can't starve
    gap_fill: bool = True  # RapidOCR scanned gap-fill; set CITADEL_GAP_FILL=0 for clean-image benchmarks (pure VLM)

    @property
    def mineru_base_url(self) -> str:
        return f"http://{self.mineru_host}:{self.mineru_port}"

    @property
    def qwen_base_url(self) -> str:
        return f"http://{self.qwen_host}:{self.qwen_port}/v1"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
