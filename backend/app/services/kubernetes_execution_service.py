from __future__ import annotations

import asyncio
import logging
import time
import uuid

from app.services.execution_service import ContainerSpec, ExecutionResult

logger = logging.getLogger(__name__)

try:
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    _K8S_AVAILABLE = True
except ImportError:
    _K8S_AVAILABLE = False
    logger.warning("kubernetes SDK not installed; KubernetesExecutionService unavailable")


class KubernetesExecutionService:
    """Runs job containers as K8s Jobs; K8s scheduler decides which Node to use.

    batch_v1 / core_v1 can be injected for unit tests without a real cluster.
    In production, pass nothing and the service auto-loads kubeconfig (in-cluster
    or ~/.kube/config).
    """

    def __init__(
        self,
        namespace: str = "default",
        poll_interval: float = 2.0,
        batch_v1=None,
        core_v1=None,
    ):
        self.namespace = namespace
        self.poll_interval = poll_interval
        self._batch_v1 = batch_v1
        self._core_v1 = core_v1

    def _ensure_clients(self) -> None:
        if self._batch_v1 is not None:
            return
        if not _K8S_AVAILABLE:
            raise RuntimeError("kubernetes SDK not installed")
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        self._batch_v1 = k8s_client.BatchV1Api()
        self._core_v1 = k8s_client.CoreV1Api()

    def run(self, spec: ContainerSpec) -> ExecutionResult:
        try:
            return self._run_k8s_job(spec)
        except Exception as exc:
            return ExecutionResult(success=False, stdout="", stderr=str(exc))

    async def run_async(self, spec: ContainerSpec) -> ExecutionResult:
        """Non-blocking async wrapper: runs the K8s polling loop in a thread
        so the event loop stays free to process other coroutines concurrently."""
        return await asyncio.to_thread(self.run, spec)

    def _build_job_manifest(self, spec: ContainerSpec, job_name: str):
        resources: dict[str, str] = {}
        if spec.cpu is not None:
            resources["cpu"] = f"{int(spec.cpu * 1000)}m"
        if spec.memory_mb is not None:
            resources["memory"] = f"{spec.memory_mb}Mi"

        container_kwargs: dict = dict(
            name="job",
            image=spec.image,
            env=[
                k8s_client.V1EnvVar(name=k, value=v)
                for k, v in (spec.env or {}).items()
            ],
            resources=k8s_client.V1ResourceRequirements(
                requests=resources or None,
                limits=resources or None,
            ),
        )
        if spec.command:
            container_kwargs["command"] = spec.command
        if spec.working_dir:
            container_kwargs["working_dir"] = spec.working_dir

        container = k8s_client.V1Container(**container_kwargs)

        return k8s_client.V1Job(
            metadata=k8s_client.V1ObjectMeta(
                name=job_name,
                labels={"app": "dass", "component": "job"},
            ),
            spec=k8s_client.V1JobSpec(
                template=k8s_client.V1PodTemplateSpec(
                    spec=k8s_client.V1PodSpec(
                        containers=[container],
                        restart_policy="Never",
                    )
                ),
                backoff_limit=0,
                active_deadline_seconds=spec.timeout_seconds,
                ttl_seconds_after_finished=600,
            ),
        )

    def _run_k8s_job(self, spec: ContainerSpec) -> ExecutionResult:
        self._ensure_clients()

        job_name = f"dass-job-{uuid.uuid4().hex[:8]}"
        manifest = self._build_job_manifest(spec, job_name)

        self._batch_v1.create_namespaced_job(self.namespace, manifest)
        logger.info("Created K8s Job name=%s namespace=%s", job_name, self.namespace)

        deadline = time.monotonic() + spec.timeout_seconds + 30
        while time.monotonic() < deadline:
            status = self._batch_v1.read_namespaced_job_status(job_name, self.namespace)
            succeeded = status.status.succeeded or 0
            failed = status.status.failed or 0

            if succeeded > 0:
                stdout, stderr = self._get_pod_logs(job_name)
                logger.info("K8s Job succeeded name=%s", job_name)
                return ExecutionResult(success=True, stdout=stdout, stderr=stderr, exit_code=0)

            if failed > 0:
                stdout, stderr = self._get_pod_logs(job_name)
                logger.warning("K8s Job failed name=%s", job_name)
                return ExecutionResult(success=False, stdout=stdout, stderr=stderr, exit_code=1)

            time.sleep(self.poll_interval)

        logger.error("K8s Job timed out name=%s", job_name)
        return ExecutionResult(
            success=False,
            stdout="",
            stderr=f"Container execution timed out after {spec.timeout_seconds}s",
        )

    def _get_pod_logs(self, job_name: str) -> tuple[str, str]:
        try:
            pods = self._core_v1.list_namespaced_pod(
                self.namespace,
                label_selector=f"job-name={job_name}",
            )
            if not pods.items:
                return "", ""
            pod_name = pods.items[0].metadata.name
            logs = self._core_v1.read_namespaced_pod_log(
                pod_name, self.namespace, container="job"
            )
            # K8s 的 pod log 是 stdout+stderr 合併後的單一串流,API 無法分離兩者。
            # 因此把整段 log 當成 stdout 回傳、stderr 留空——這是 K8s 後端的先天限制,
            # 不是漏接 stderr。Docker 後端(subprocess)才有分離的 stdout/stderr。
            return logs or "", ""
        except Exception:
            logger.exception("Failed to get pod logs for job=%s", job_name)
            return "", ""
