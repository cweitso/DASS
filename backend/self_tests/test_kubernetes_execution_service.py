"""Unit tests for KubernetesExecutionService.

All tests use injected mock clients (batch_v1, core_v1) so no real cluster is
needed. The kubernetes SDK objects (V1Job, V1Container, etc.) are constructed
normally — only the API calls are mocked.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.execution_service import ContainerSpec
from app.services.kubernetes_execution_service import KubernetesExecutionService


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _job_status(succeeded: int = 0, failed: int = 0) -> MagicMock:
    s = MagicMock()
    s.status.succeeded = succeeded
    s.status.failed = failed
    return s


def _pod_list(*names: str) -> MagicMock:
    pl = MagicMock()
    pl.items = []
    for n in names:
        p = MagicMock()
        p.metadata.name = n
        pl.items.append(p)
    return pl


def _make_svc(
    batch_responses: list,
    pod_names: tuple[str, ...] = ("pod-abc",),
    log_output: str = "task output\n",
) -> tuple[KubernetesExecutionService, MagicMock, MagicMock]:
    """Return (service, mock_batch, mock_core) with pre-configured responses."""
    mock_batch = MagicMock()
    mock_core = MagicMock()
    mock_batch.read_namespaced_job_status.side_effect = batch_responses
    mock_core.list_namespaced_pod.return_value = _pod_list(*pod_names)
    mock_core.read_namespaced_pod_log.return_value = log_output
    svc = KubernetesExecutionService(
        namespace="test-ns",
        poll_interval=0,
        batch_v1=mock_batch,
        core_v1=mock_core,
    )
    return svc, mock_batch, mock_core


def _capture_manifest(spec: ContainerSpec):
    """Run spec through the service and return the V1Job passed to create_namespaced_job."""
    svc, mock_batch, _ = _make_svc([_job_status(succeeded=1)], log_output="")
    svc.run(spec)
    return mock_batch.create_namespaced_job.call_args.args[1]


# ---------------------------------------------------------------------------
# Success / failure paths
# ---------------------------------------------------------------------------

class TestExecutionPaths:
    def test_successful_job_returns_success(self):
        svc, _, _ = _make_svc([_job_status(succeeded=1)])
        result = svc.run(ContainerSpec(image="alpine:latest", timeout_seconds=60))
        assert result.success is True
        assert result.exit_code == 0

    def test_successful_job_captures_pod_logs_as_stdout(self):
        svc, _, _ = _make_svc([_job_status(succeeded=1)], log_output="hello from job\n")
        result = svc.run(ContainerSpec(image="alpine:latest", timeout_seconds=60))
        assert result.stdout == "hello from job\n"

    def test_failed_job_returns_failure(self):
        svc, _, _ = _make_svc([_job_status(failed=1)])
        result = svc.run(ContainerSpec(image="alpine:latest", timeout_seconds=60))
        assert result.success is False
        assert result.exit_code == 1

    def test_polls_until_succeeded(self):
        # First poll: pending; second poll: succeeded
        svc, mock_batch, _ = _make_svc([_job_status(0, 0), _job_status(succeeded=1)])
        result = svc.run(ContainerSpec(image="alpine:latest", timeout_seconds=60))
        assert result.success is True
        assert mock_batch.read_namespaced_job_status.call_count == 2

    def test_job_created_before_polling(self):
        svc, mock_batch, _ = _make_svc([_job_status(succeeded=1)])
        svc.run(ContainerSpec(image="alpine:latest", timeout_seconds=60))
        mock_batch.create_namespaced_job.assert_called_once()
        mock_batch.read_namespaced_job_status.assert_called_once()

    def test_namespace_passed_to_create(self):
        svc, mock_batch, _ = _make_svc([_job_status(succeeded=1)])
        svc.run(ContainerSpec(image="img", timeout_seconds=10))
        namespace_arg = mock_batch.create_namespaced_job.call_args.args[0]
        assert namespace_arg == "test-ns"

    def test_api_exception_returns_failure(self):
        mock_batch = MagicMock()
        mock_core = MagicMock()
        mock_batch.create_namespaced_job.side_effect = Exception("connection refused")
        svc = KubernetesExecutionService(namespace="ns", poll_interval=0, batch_v1=mock_batch, core_v1=mock_core)
        result = svc.run(ContainerSpec(image="alpine", timeout_seconds=10))
        assert result.success is False
        assert "connection refused" in result.stderr


# ---------------------------------------------------------------------------
# Job manifest construction
# ---------------------------------------------------------------------------

class TestJobManifest:
    def test_cpu_converted_to_millicores(self):
        manifest = _capture_manifest(ContainerSpec(image="img", cpu=0.3, timeout_seconds=10))
        container = manifest.spec.template.spec.containers[0]
        assert container.resources.requests["cpu"] == "300m"
        assert container.resources.limits["cpu"] == "300m"

    def test_memory_mb_converted_to_mi(self):
        manifest = _capture_manifest(ContainerSpec(image="img", memory_mb=512, timeout_seconds=10))
        container = manifest.spec.template.spec.containers[0]
        assert container.resources.requests["memory"] == "512Mi"
        assert container.resources.limits["memory"] == "512Mi"

    def test_no_resources_leaves_none(self):
        manifest = _capture_manifest(ContainerSpec(image="img", timeout_seconds=10))
        container = manifest.spec.template.spec.containers[0]
        assert container.resources.requests is None
        assert container.resources.limits is None

    def test_timeout_becomes_active_deadline_seconds(self):
        manifest = _capture_manifest(ContainerSpec(image="img", timeout_seconds=120))
        assert manifest.spec.active_deadline_seconds == 120

    def test_env_vars_mapped_to_k8s_env(self):
        manifest = _capture_manifest(
            ContainerSpec(image="img", env={"FOO": "bar", "BAZ": "qux"}, timeout_seconds=10)
        )
        container = manifest.spec.template.spec.containers[0]
        env_map = {e.name: e.value for e in container.env}
        assert env_map["FOO"] == "bar"
        assert env_map["BAZ"] == "qux"

    def test_command_propagated(self):
        manifest = _capture_manifest(
            ContainerSpec(image="img", command=["python", "run.py"], timeout_seconds=10)
        )
        container = manifest.spec.template.spec.containers[0]
        assert container.command == ["python", "run.py"]

    def test_working_dir_propagated(self):
        manifest = _capture_manifest(
            ContainerSpec(image="img", working_dir="/app", timeout_seconds=10)
        )
        container = manifest.spec.template.spec.containers[0]
        assert container.working_dir == "/app"

    def test_restart_policy_never(self):
        manifest = _capture_manifest(ContainerSpec(image="img", timeout_seconds=10))
        assert manifest.spec.template.spec.restart_policy == "Never"

    def test_backoff_limit_zero(self):
        # K8s must not retry the Pod on failure — retries are owned by WorkerService
        manifest = _capture_manifest(ContainerSpec(image="img", timeout_seconds=10))
        assert manifest.spec.backoff_limit == 0

    def test_job_names_are_unique(self):
        names: set[str] = set()
        for _ in range(5):
            mock_batch = MagicMock()
            mock_core = MagicMock()
            mock_batch.read_namespaced_job_status.return_value = _job_status(succeeded=1)
            mock_core.list_namespaced_pod.return_value = _pod_list("pod-1")
            mock_core.read_namespaced_pod_log.return_value = ""
            svc = KubernetesExecutionService(namespace="ns", poll_interval=0, batch_v1=mock_batch, core_v1=mock_core)
            svc.run(ContainerSpec(image="img", timeout_seconds=10))
            names.add(mock_batch.create_namespaced_job.call_args.args[1].metadata.name)
        assert len(names) == 5

    def test_job_name_starts_with_dass_job(self):
        manifest = _capture_manifest(ContainerSpec(image="img", timeout_seconds=10))
        assert manifest.metadata.name.startswith("dass-job-")


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

class TestTimeout:
    def test_timeout_returns_failure(self, monkeypatch):
        call_count = 0

        def fake_monotonic():
            nonlocal call_count
            call_count += 1
            # call 1 → sets deadline; call 2 onward → past deadline
            return 0.0 if call_count == 1 else 100.0

        monkeypatch.setattr("app.services.kubernetes_execution_service.time.monotonic", fake_monotonic)
        monkeypatch.setattr("app.services.kubernetes_execution_service.time.sleep", lambda _: None)

        mock_batch = MagicMock()
        mock_core = MagicMock()
        mock_batch.read_namespaced_job_status.return_value = _job_status(0, 0)

        svc = KubernetesExecutionService(namespace="ns", poll_interval=0, batch_v1=mock_batch, core_v1=mock_core)
        result = svc.run(ContainerSpec(image="alpine", timeout_seconds=5))

        assert result.success is False
        assert "timed out after 5s" in result.stderr
