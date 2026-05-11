from __future__ import annotations

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


def test_execution_service_http_uses_json_for_dict_body(monkeypatch):
    captured = {}

    class DummyResponse:
        is_success = True
        status_code = 200
        text = "ok"

    class DummyClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, method, url, headers=None, content=None, json=None):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            captured["content"] = content
            captured["json"] = json
            return DummyResponse()

    monkeypatch.setattr("app.services.execution_service.httpx.Client", lambda timeout: DummyClient(timeout))

    service = ExecutionService()
    result = service.run(
        "http",
        {
            "method": "POST",
            "url": "https://example.com",
            "headers": {"X-Trace": "1"},
            "body": {"hello": "world"},
            "timeout_seconds": 3,
        },
    )

    assert result == ExecutionResult(success=True, stdout="status=200\nok", stderr="")
    assert captured["method"] == "POST"
    assert captured["url"] == "https://example.com"
    assert captured["headers"] == {"X-Trace": "1"}
    assert captured["content"] is None
    assert captured["json"] == {"hello": "world"}


def test_execution_service_shell_reports_exit_code(monkeypatch):
    CompletedProcess = namedtuple("CompletedProcess", ["returncode", "stdout", "stderr"])

    monkeypatch.setattr(
        "app.services.execution_service.subprocess.run",
        lambda *args, **kwargs: CompletedProcess(returncode=1, stdout="hello\n", stderr="boom\n"),
    )

    service = ExecutionService()
    result = service.run("shell", {"command": "echo hello", "timeout_seconds": 5})

    assert result.success is False
    assert result.stdout == "hello\n"
    assert result.stderr == "boom\n"
    assert result.exit_code == 1


def test_execution_service_rejects_unsupported_action_type():
    service = ExecutionService()

    with pytest.raises(HTTPException) as exc_info:
        service.run("email", {})

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Unsupported action type"
