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
    cors_origins: str = "http://localhost,http://127.0.0.1,https://localhost:8443,https://127.0.0.1:8443,http://localhost:3000,http://127.0.0.1:3000"

    database_url: str = "postgresql+psycopg://dass:dass@postgres:5432/dass"
    database_echo: bool = False
    
    replica_database_url: str | None = None # (允許為空，當作單機模式的退路)

    queue_backend: Literal["sqs", "memory"] = "sqs"
    queue_name: str = "dass-tasks"
    queue_name_normal: str = "dass-tasks-normal"
    queue_name_scheduled: str = "dass-tasks-scheduled"
    queue_name_retry: str = "dass-tasks-retry"
    aws_region: str = "us-east-1"
    aws_access_key_id: str = Field(default="dass")
    aws_secret_access_key: str = Field(default="dass")
    aws_session_token: str | None = None
    sqs_endpoint_url: str | None = "http://localstack:4566"

    scheduler_interval_seconds: int = 30
    scheduler_locked_task_scan_seconds: int = 5
    # 短 visibility + worker 端 heartbeat 動態延長；長 job 也安全，crash 時 30s 內可被 reclaim。
    worker_visibility_timeout_seconds: int = 30
    autoscaler_interval_seconds: int = 30
    worker_id: str = "worker"
    task_timeout_seconds: int = 300
    shell_execution_enabled: bool = True
    http_request_timeout_seconds: int = 30

    execution_backend: Literal["docker", "kubernetes"] = "docker"
    k8s_namespace: str = "default"
    k8s_poll_interval_seconds: float = 2.0
    docker_network: str | None = None

    # 危險的 VM admin API（POST /vms 透過 host docker.sock 起 worker 容器）預設關閉；
    # 真要手動用時以 DASS_VM_ADMIN_API_ENABLED=true 顯式開啟。autoscaler 不走這個 HTTP
    # 端點（它直接呼叫 vm_service），所以關掉不影響自動擴縮。
    vm_admin_api_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
