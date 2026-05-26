"""VMService — real Docker-backed worker scaling.

Spawns / terminates worker *containers* by cloning the config of an existing
worker container (the one compose started as `dass_alt-worker-1`). Autoscaled
siblings are labelled `com.dass.autoscaled=true` so scale-down only touches them
and never kills the baseline compose worker.

Falls back to in-memory mock mode if the Docker socket isn't reachable — e.g.
unit tests or local dev without `/var/run/docker.sock`.
"""
from __future__ import annotations

import logging
import os
from typing import List
from uuid import uuid4

logger = logging.getLogger(__name__)

try:
    import docker
    from docker.errors import DockerException, NotFound
    _DOCKER_IMPORT_OK = True
except ImportError:
    _DOCKER_IMPORT_OK = False
    docker = None  # type: ignore[assignment]


# Labels used to discover worker containers. The baseline compose worker carries
# the first two; autoscaled siblings also carry `com.dass.autoscaled=true`.
_PROJECT_LABEL = "com.dass.project=dass"
_SERVICE_LABEL = "com.dass.service=worker"
_AUTOSCALED_LABEL = "com.dass.autoscaled=true"

_TEMPLATE_CONTAINER_NAME = os.environ.get("DASS_WORKER_TEMPLATE", "dass_alt-worker-1")


class VMService:
    """Manages worker container fleet."""

    def __init__(self) -> None:
        self._client = None
        self._mock_vms: list[str] = []
        if not _DOCKER_IMPORT_OK:
            logger.warning("docker SDK not installed; VMService in mock mode")
            return
        try:
            self._client = docker.from_env()
            self._client.ping()
            logger.info("VMService connected to Docker daemon")
        except DockerException as e:
            logger.warning("Docker not reachable (%s); VMService in mock mode", e)
            self._client = None

    # ── Discovery ───────────────────────────────────────────────────────────
    def _list_workers(self, autoscaled_only: bool = False):
        if self._client is None:
            return []
        labels = [_PROJECT_LABEL, _SERVICE_LABEL]
        if autoscaled_only:
            labels.append(_AUTOSCALED_LABEL)
        try:
            return self._client.containers.list(filters={"label": labels})
        except DockerException:
            logger.exception("docker list failed")
            return []

    def _template_container(self):
        """Find a baseline worker to clone config from."""
        if self._client is None:
            return None
        try:
            return self._client.containers.get(_TEMPLATE_CONTAINER_NAME)
        except NotFound:
            pass
        candidates = self._list_workers()
        return candidates[0] if candidates else None

    # ── Public API ──────────────────────────────────────────────────────────
    def get_active_vms(self) -> List[str]:
        """Return names of all running worker containers (baseline + autoscaled)."""
        if self._client is None:
            return list(self._mock_vms)
        return [c.name for c in self._list_workers()]

    def create_vms(self, count: int, instance_type: str = "t3.micro") -> List[str]:
        """Spawn N new worker containers cloned from the template."""
        if count <= 0:
            return []

        if self._client is None:
            new = [f"i-{uuid4().hex[:8]}" for _ in range(count)]
            self._mock_vms.extend(new)
            logger.info("[mock] create_vms(%d) → %s", count, new)
            return new

        template = self._template_container()
        if template is None:
            logger.error("create_vms: no template worker container available")
            return []

        attrs = template.attrs
        image = attrs["Config"]["Image"]
        env_list = attrs["Config"].get("Env") or []
        cmd = attrs["Config"].get("Cmd")
        networks = attrs.get("NetworkSettings", {}).get("Networks", {})
        network_name = next(iter(networks.keys()), "dass_alt_dass-net")

        env_dict: dict[str, str] = {}
        for entry in env_list:
            k, _, v = entry.partition("=")
            if k:
                env_dict[k] = v

        created: list[str] = []
        for _ in range(count):
            suffix = uuid4().hex[:8]
            name = f"dass_alt-worker-autoscaled-{suffix}"
            env_dict["DASS_WORKER_ID"] = f"worker-as-{suffix}"
            try:
                container = self._client.containers.run(
                    image,
                    command=cmd,
                    name=name,
                    detach=True,
                    environment=env_dict,
                    network=network_name,
                    labels={
                        "com.dass.project": "dass",
                        "com.dass.service": "worker",
                        "com.dass.autoscaled": "true",
                    },
                    volumes={
                        "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
                    },
                    restart_policy={"Name": "unless-stopped"},
                )
                created.append(container.name)
                logger.info("✨ scaled up worker container=%s", container.name)
            except DockerException:
                logger.exception("Failed to start worker container=%s", name)
        return created

    def terminate_vms(self, count: int) -> List[str]:
        """Stop+remove N autoscaled workers, oldest first. Never touches baseline."""
        if count <= 0:
            return []

        if self._client is None:
            removed: list[str] = []
            while count > 0 and self._mock_vms:
                removed.append(self._mock_vms.pop(0))
                count -= 1
            logger.info("[mock] terminate_vms → %s", removed)
            return removed

        autoscaled = self._list_workers(autoscaled_only=True)
        autoscaled.sort(key=lambda c: c.attrs.get("Created", ""))
        victims = autoscaled[:count]

        terminated: list[str] = []
        for c in victims:
            try:
                # SIGTERM then 1s grace before SIGKILL. Worker has no SIGTERM
                # handler so it'll be force-killed anyway; in-flight tasks
                # recover via scheduler.recover_orphans within DB lock TTL
                # (= DASS_WORKER_VISIBILITY_TIMEOUT_SECONDS = 30s).
                c.stop(timeout=1)
                c.remove(force=False)
                terminated.append(c.name)
                logger.info("🗑  scaled down worker container=%s", c.name)
            except DockerException:
                logger.exception("Failed to stop worker container=%s", c.name)
        return terminated


# Singleton for app code.
vm_service = VMService()
