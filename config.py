import logging
import os
from functools import lru_cache
from pathlib import Path

from loguru import logger as loguru_logger
from pydantic_settings import BaseSettings, SettingsConfigDict
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

CPU_THIRD = max((os.cpu_count() or 3) // 3, 1)  # per-worker CPU stages: a third of the cores, at least 1


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    loguru_logger.disable("mineru_vl_utils")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CITADEL_", env_file=".env", extra="ignore")

    mineru_host: str = "localhost"
    mineru_port: int = 8099

    qwen_host: str = "localhost"
    qwen_port: int = 11434  # ollama OpenAI-compatible endpoint (vLLM hung on Blackwell sm_120 FlashInfer kernels)
    qwen_model: str = "qwen3:8b"  # exact ollama tag (`ollama list`)

    embed_model: str = "BAAI/bge-m3"
    embed_device: str = "cuda"

    redis_url: str = "redis://localhost:6379/0"

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str
    postgres_db: str = "citadel"

    tree_store_dir: Path = Path("data/trees")

    worker_id: str = "0"  # stable per-replica id → deterministic Redis consumer names; set distinctly per replica

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


@lru_cache
def get_embedder() -> SentenceTransformer:
    settings = get_settings()
    logger.info("loading embedder model=%s device=%s", settings.embed_model, settings.embed_device)
    model = SentenceTransformer(settings.embed_model, device=settings.embed_device)
    logger.info("embedder loaded model=%s", settings.embed_model)
    return model
