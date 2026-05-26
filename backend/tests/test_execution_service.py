from __future__ import annotations

import subprocess

from app.services.execution_service import ContainerSpec, ExecutionService


class _CompletedProcess:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_execution_service_runs_container(monkeypatch):
    captured_args = None
    captured_kwargs = None

    def mock_run(*args, **kwargs):
        nonlocal captured_args, captured_kwargs
        captured_args = args[0]
        captured_kwargs = kwargs
        return _CompletedProcess(stdout="container output")

    monkeypatch.setattr("app.services.execution_service.subprocess.run", mock_run)

    service = ExecutionService()
    spec = ContainerSpec(
        image="my-alpine:latest",
        command=["echo", "hello"],
        env={"TEST_VAR": "123"},
        timeout_seconds=10,
    )
    result = service.run(spec)

    assert result.success is True
    assert result.stdout == "container output"
    assert captured_args[:3] == ["docker", "run", "--rm"]
    assert "my-alpine:latest" in captured_args
    assert "-e" in captured_args
    assert "TEST_VAR=123" in captured_args
    image_idx = captured_args.index("my-alpine:latest")
    assert captured_args[image_idx + 1 :] == ["echo", "hello"]
    assert captured_kwargs["timeout"] == 10


def test_execution_service_applies_resource_limits(monkeypatch):
    captured_args = None

    def mock_run(*args, **kwargs):
        nonlocal captured_args
        captured_args = args[0]
        return _CompletedProcess()

    monkeypatch.setattr("app.services.execution_service.subprocess.run", mock_run)

    service = ExecutionService()
    service.run(
        ContainerSpec(
            image="alpine:3",
            cpu=0.5,
            memory_mb=128,
            working_dir="/work",
        )
    )

    assert "--cpus" in captured_args
    assert "0.5" in captured_args
    assert "--memory" in captured_args
    assert "128m" in captured_args
    assert "-w" in captured_args
    assert "/work" in captured_args


def test_execution_service_timeout_returns_failure(monkeypatch):
    def mock_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr("app.services.execution_service.subprocess.run", mock_run)

    service = ExecutionService()
    result = service.run(ContainerSpec(image="busybox", timeout_seconds=2))

    assert result.success is False
    assert "timed out after 2s" in result.stderr


def test_execution_service_invalid_env_key_returns_failure():
    service = ExecutionService()
    result = service.run(ContainerSpec(image="busybox", env={"FOO=BAR": "baz"}))

    assert result.success is False
    assert "Invalid environment variable key" in result.stderr


def test_execution_service_non_zero_exit_marks_failure(monkeypatch):
    def mock_run(*args, **kwargs):
        return _CompletedProcess(stdout="", stderr="boom", returncode=2)

    monkeypatch.setattr("app.services.execution_service.subprocess.run", mock_run)

    service = ExecutionService()
    result = service.run(ContainerSpec(image="busybox"))

    assert result.success is False
    assert result.exit_code == 2
    assert result.stderr == "boom"
