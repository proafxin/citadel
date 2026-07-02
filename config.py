import logging
import os
from functools import lru_cache
from pathlib import Path

from loguru import logger as loguru_logger
from pydantic_settings import BaseSettings, SettingsConfigDict
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

CPU_THIRD = max((os.cpu_count() or 3) // 3, 1)  # per-worker CPU stages: a third of the cores, at least 1
CPU_HALF = max((os.cpu_count() or 2) // 2, 1)  # heavier CPU stages (normalize, paginate): half the cores

QWEN_MODEL = "qwen"
EMBED_MODEL = "BAAI/bge-m3"
EMBED_DEVICE = "cuda"


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    loguru_logger.disable("mineru_vl_utils")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CITADEL_", env_file=".env", extra="ignore")

    mineru_host: str = "localhost"
    mineru_port: int = 8099

    qwen_host: str = "localhost"
    qwen_port: int = 8100  # vLLM OpenAI-compatible server (Qwen3.5-4B, guided decoding)

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

    @property
    def pg_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_embedder() -> SentenceTransformer:
    logger.info("loading embedder model=%s device=%s", EMBED_MODEL, EMBED_DEVICE)
    model = SentenceTransformer(EMBED_MODEL, device=EMBED_DEVICE, model_kwargs={"torch_dtype": "bfloat16"})
    logger.info("embedder loaded model=%s", EMBED_MODEL)
    return model
