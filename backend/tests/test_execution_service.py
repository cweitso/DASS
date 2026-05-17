from __future__ import annotations

import json
import subprocess
from collections import namedtuple
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app.services.execution_service import ExecutionResult, ExecutionService
from app.utils.cron import next_cron_time


def test_next_cron_time_preserves_timezone_for_naive_cron_result(monkeypatch):
    base_time = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    naive_next_time = datetime(2026, 4, 29, 12, 5)

    class FakeIterator:
        def get_next(self, _type):
            return naive_next_time

    monkeypatch.setattr("app.utils.cron.croniter", lambda expression, base_time: FakeIterator())

    result = next_cron_time("* * * * *", base_time)

    assert result == naive_next_time.replace(tzinfo=UTC)
    assert result.tzinfo == UTC


def test_execution_service_container_action(monkeypatch):
    captured_args = None
    captured_kwargs = None

    class CompletedProcess:
        returncode = 0
        stdout = "container output"
        stderr = ""

    def mock_run(*args, **kwargs):
        nonlocal captured_args, captured_kwargs
        captured_args = args[0]
        captured_kwargs = kwargs
        return CompletedProcess()

    monkeypatch.setattr("app.services.execution_service.subprocess.run", mock_run)

    service = ExecutionService()
    result = service.run(
        "container",
        {
            "image": "my-alpine:latest",
            "command": "echo hello",
            "env": {"TEST_VAR": "123"},
            "timeout_seconds": 10,
        },
    )

    assert result.success is True
    assert result.stdout == "container output"
    assert "docker" in captured_args
    assert "run" in captured_args
    assert "my-alpine:latest" in captured_args
    assert "-e" in captured_args
    assert "TEST_VAR=123" in captured_args
    assert captured_kwargs["timeout"] == 10


def test_execution_service_fallback_action(monkeypatch):
    captured_args = None

    class CompletedProcess:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def mock_run(*args, **kwargs):
        nonlocal captured_args
        captured_args = args[0]
        return CompletedProcess()

    monkeypatch.setattr("app.services.execution_service.subprocess.run", mock_run)

    service = ExecutionService()
    result = service.run(
        "http",
        {
            "method": "POST",
            "url": "https://example.com",
            "timeout_seconds": 3,
        },
    )

    assert result.success is True
    assert "allen/default-runner:latest" in captured_args
    assert "-e" in captured_args
    assert "ACTION_TYPE=http" in captured_args
    
    # Check that ACTION_CONFIG_JSON was passed
    config_json = json.dumps({"method": "POST", "url": "https://example.com", "timeout_seconds": 3})
    assert f"ACTION_CONFIG_JSON={config_json}" in captured_args


def test_execution_service_timeout(monkeypatch):
    def mock_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr("app.services.execution_service.subprocess.run", mock_run)

    service = ExecutionService()
    result = service.run("container", {"image": "busybox", "timeout_seconds": 2})

    assert result.success is False
    assert "timed out after 2s" in result.stderr


def test_execution_service_invalid_env_key():
    service = ExecutionService()
    result = service.run(
        "container",
        {
            "image": "busybox",
            "env": {"FOO=BAR": "baz"},
        },
    )

    assert result.success is False
    assert "Invalid environment variable key" in result.stderr
