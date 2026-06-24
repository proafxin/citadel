import logging
from functools import lru_cache
from pathlib import Path

from loguru import logger as loguru_logger
from pydantic_settings import BaseSettings, SettingsConfigDict


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    loguru_logger.disable("mineru_vl_utils")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CITADEL_", env_file=".env", extra="ignore")

    mineru_host: str = "localhost"
    mineru_port: int = 8099

    redis_url: str = "redis://localhost:6379/0"

    data_dir: Path = Path("data")  # raw/<doc_id>, normalized/<doc_id>, results/<doc_id>.json

    render_dpi: int = 200
    ocr_concurrency: int = 64  # client-side semaphore; keeps vLLM max-num-seqs (128) fed

    @property
    def mineru_base_url(self) -> str:
        return f"http://{self.mineru_host}:{self.mineru_port}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
