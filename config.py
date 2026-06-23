from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CITADEL_", env_file=".env", extra="ignore")

    vllm_base_url: str = "http://localhost:8099/v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
