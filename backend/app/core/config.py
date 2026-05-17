from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env lives at the repo root; resolve absolutely so cwd doesn't matter.
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DASS_", env_file=_ENV_FILE, extra="ignore")

    app_name: str = "dass"
    environment: str = "local"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    database_url: str = "postgresql+psycopg://dass:dass@postgres:5432/dass"
    database_echo: bool = False

    queue_backend: Literal["sqs", "memory"] = "sqs"
    queue_name: str = "dass-tasks"
    queue_name_normal: str = "dass-tasks-normal"
    queue_name_retry: str = "dass-tasks-retry"
    aws_region: str = "us-east-1"
    aws_access_key_id: str = Field(default="dass")
    aws_secret_access_key: str = Field(default="dass")
    aws_session_token: str | None = None
    sqs_endpoint_url: str | None = "http://localstack:4566"

    scheduler_interval_seconds: int = 5
    scheduler_locked_task_scan_seconds: int = 5
    worker_visibility_timeout_seconds: int = 300
    worker_id: str = "worker"
    task_timeout_seconds: int = 300
    shell_execution_enabled: bool = True
    http_request_timeout_seconds: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
