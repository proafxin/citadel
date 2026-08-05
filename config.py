import logging
import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sentence_transformers import SentenceTransformer
from transformers import AutoTokenizer, PreTrainedTokenizerBase

logger = logging.getLogger(__name__)

CPU_THIRD = max((os.cpu_count() or 3) // 3, 1)
CPU_EIGHTH = max((os.cpu_count() or 8) // 8, 1)

QWEN_MODEL = "qwen"
QWEN_HF_REPO = "Qwen/Qwen3.5-9B"
QWEN_CACHE_DIR = Path.home() / ".cache" / "citadel-qwen" / "hub"
PADDLEOCR_MODEL = "paddleocr-vl"
EMBED_MODEL = "BAAI/bge-m3"
EMBED_DEVICE = "cuda"
EMBED_MAX_TOKENS = 8192


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CITADEL_", env_file=".env", extra="ignore")

    paddleocr_host: str = "localhost"
    paddleocr_port: int = 8099

    qwen_host: str = "localhost"
    qwen_port: int = 8100

    redis_url: str = "redis://localhost:6379/0"

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str
    postgres_db: str = "citadel"

    tree_store_dir: Path = Path("data/trees")

    worker_id: str

    @property
    def paddleocr_base_url(self) -> str:
        return f"http://{self.paddleocr_host}:{self.paddleocr_port}"

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
    model = SentenceTransformer(
        EMBED_MODEL,
        device=EMBED_DEVICE,
        model_kwargs={"torch_dtype": "bfloat16", "attn_implementation": "sdpa"},
    )
    logger.info("embedder loaded model=%s", EMBED_MODEL)
    return model


@lru_cache
def get_embed_tokenizer() -> PreTrainedTokenizerBase:
    return AutoTokenizer.from_pretrained(EMBED_MODEL)
