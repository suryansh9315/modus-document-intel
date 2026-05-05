"""
Application configuration via pydantic-settings.
All settings are read from environment variables / .env file.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # MongoDB
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db_name: str = "modus_db"

    # DuckDB
    duckdb_path: str = "/data/modus.duckdb"

    # Cerebras
    cerebras_api_key: str = ""

    # File storage
    upload_dir: str = "/data/uploads"

    # CORS
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:3001"]

    # Token budget for query agents
    token_budget: int = 120_000


settings = Settings()
