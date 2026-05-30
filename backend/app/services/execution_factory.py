from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.services.execution_service import ExecutionService


@lru_cache(maxsize=1)
def get_execution_service():
    """Return the configured execution backend (docker or kubernetes).

    Cached as a process-level singleton so K8s API clients (and their
    underlying urllib3 connection pools) are created once and reused
    across all tasks — preventing connection-pool accumulation under load.
    """
    settings = get_settings()
    if settings.execution_backend == "kubernetes":
        from app.services.kubernetes_execution_service import KubernetesExecutionService
        return KubernetesExecutionService(
            namespace=settings.k8s_namespace,
            poll_interval=settings.k8s_poll_interval_seconds,
        )
    return ExecutionService(network=settings.docker_network)
