import logging
import os
from functools import lru_cache
from pathlib import Path

from huggingface_hub import hf_hub_download
from pydantic_settings import BaseSettings, SettingsConfigDict
from tokenizers import Tokenizer

logger = logging.getLogger(__name__)

CPU_THIRD = max((os.cpu_count() or 3) // 3, 1)
CPU_EIGHTH = max((os.cpu_count() or 8) // 8, 1)

QWEN_MODEL = "qwen"
QWEN_HF_REPO = "Qwen/Qwen3.5-9B"
QWEN_CACHE_DIR = Path.home() / ".cache" / "citadel-tokenizer"
EMBED_MODEL = "BAAI/bge-m3"
EMBED_SERVED_NAME = "bge"
EMBED_MAX_TOKENS = 8192


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CITADEL_", env_file=".env", extra="ignore")

    qwen_host: str = "localhost"
    qwen_port: int = 8100

    bge_host: str = "localhost"
    bge_port: int = 8101

    redis_url: str = "redis://localhost:6379/0"

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str
    postgres_db: str = "citadel"

    tree_store_dir: Path = Path("data/trees")

    @property
    def qwen_base_url(self) -> str:
        return f"http://{self.qwen_host}:{self.qwen_port}/v1"

    @property
    def bge_base_url(self) -> str:
        return f"http://{self.bge_host}:{self.bge_port}/v1"

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
def get_embed_tokenizer() -> Tokenizer:
    tokenizer = Tokenizer.from_file(hf_hub_download(EMBED_MODEL, "tokenizer.json"))
    tokenizer.enable_truncation(max_length=EMBED_MAX_TOKENS)
    return tokenizer
