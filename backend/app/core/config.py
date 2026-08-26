from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env lives at the repo root; resolve it absolutely so the working directory
# never changes which file is loaded.
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

_DEFAULT_CORS_ORIGINS = ",".join(
    [
        "http://dass.localhost",
        "http://localhost",
        "http://127.0.0.1",
        "https://dass.localhost:8443",
        "https://localhost:8443",
        "https://127.0.0.1:8443",
        "http://dass.localhost:3000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
)


class Settings(BaseSettings):
    """Every field is read from a DASS_-prefixed environment variable."""

    model_config = SettingsConfigDict(
        env_prefix="DASS_", env_file=_ENV_FILE, extra="ignore"
    )

    log_level: str = "INFO"
    cors_origins: str = _DEFAULT_CORS_ORIGINS

    # Database. Leave replica_database_url unset to run against the primary alone;
    # the session layer then reuses one engine instead of opening a second pool.
    database_url: str = "postgresql+psycopg://dass:dass@postgres:5432/dass"
    replica_database_url: str | None = None
    database_echo: bool = False

    # Queue. Each dispatch path has its own queue so they scale independently:
    # normal = on-demand triggers, scheduled = cron dispatches, retry = re-runs.
    queue_backend: Literal["sqs", "memory"] = "sqs"
    queue_name: str = "dass-tasks"
    queue_name_normal: str = "dass-tasks-normal"
    queue_name_scheduled: str = "dass-tasks-scheduled"
    queue_name_retry: str = "dass-tasks-retry"
    aws_region: str = "us-east-1"
    aws_access_key_id: str = "dass"
    aws_secret_access_key: str = "dass"
    aws_session_token: str | None = None
    sqs_endpoint_url: str | None = "http://localstack:4566"

    # Timing. The visibility timeout is deliberately short: workers extend it with a
    # heartbeat while a task runs, so a crashed worker's task becomes reclaimable
    # within one window instead of one job duration.
    scheduler_interval_seconds: int = 30
    worker_visibility_timeout_seconds: int = 30
    autoscaler_interval_seconds: int = 30
    worker_id: str = "worker"

    # Execution backend: docker runs job containers via the host daemon, kubernetes
    # submits them as K8s Jobs.
    execution_backend: Literal["docker", "kubernetes"] = "docker"
    k8s_namespace: str = "default"
    k8s_poll_interval_seconds: float = 2.0
    docker_network: str | None = None

    # Shell actions run arbitrary commands in a container. Fine for a trusted local
    # stack, dangerous on an unauthenticated API — turn them off to reject new ones.
    shell_execution_enabled: bool = True

    # POST /vms starts worker containers through the host Docker socket, which is
    # effectively remote code execution. The autoscaler calls VMService directly, so
    # leaving this off costs nothing.
    vm_admin_api_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
