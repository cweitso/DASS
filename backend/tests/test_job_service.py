"""Unit tests for JobService — runtime_spec translation (the worker contract),
one-time vs scheduled enqueue behaviour, and runtime_spec re-sync on update.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.queue.memory import MemoryQueueClient
from app.schemas.job import JobCreate, JobUpdate
from app.services.job_service import JobService, _build_runtime_spec


def test_build_runtime_spec_http_translates_to_curl():
    spec = _build_runtime_spec(
        "http",
        {"method": "post", "url": "https://api.example.com", "headers": {"X-Token": "abc"}, "body": {"k": "v"}},
    )
    assert spec["image"].startswith("curlimages/curl")
    assert spec["command"][0] == "curl"
    assert "POST" in spec["command"]  # method upper-cased
    assert "https://api.example.com" in spec["command"]
    assert "X-Token: abc" in spec["command"]


def test_build_runtime_spec_shell_wraps_in_sh_c():
    spec = _build_runtime_spec("shell", {"command": "echo hi", "timeout_seconds": 7})
    assert spec["image"] == "alpine:3"
    assert spec["command"] == ["sh", "-c", "echo hi"]
    assert spec["timeout_seconds"] == 7


def test_build_runtime_spec_unsupported_action_type_raises_422():
    with pytest.raises(HTTPException) as exc:
        _build_runtime_spec("ftp", {})
    assert exc.value.status_code == 422


def test_create_one_time_job_auto_enqueues_to_normal_queue(db_session, monkeypatch):
    queue = MemoryQueueClient()
    monkeypatch.setattr("app.services.job_service.get_normal_queue_client", lambda: queue)

    job = JobService(db_session).create_job(
        JobCreate(name="u-onetime", action_type="shell", action_config={"command": "true"})
    )
    assert job.job_type == "normal"
    assert job.next_fire_at is None
    assert queue._queue.qsize() == 1  # one-time job fires immediately


def test_create_scheduled_job_does_not_enqueue(db_session, monkeypatch):
    queue = MemoryQueueClient()
    monkeypatch.setattr("app.services.job_service.get_normal_queue_client", lambda: queue)

    job = JobService(db_session).create_job(
        JobCreate(
            name="u-scheduled",
            cron_expression="*/5 * * * *",
            action_type="shell",
            action_config={"command": "true"},
        )
    )
    assert job.job_type == "scheduled"
    assert job.next_fire_at is not None
    assert queue._queue.qsize() == 0  # scheduler owns dispatch, not create_job


def test_update_resyncs_runtime_spec_when_action_config_changes(db_session, monkeypatch):
    monkeypatch.setattr("app.services.job_service.get_normal_queue_client", lambda: MemoryQueueClient())
    service = JobService(db_session)
    job = service.create_job(
        JobCreate(
            name="u-update",
            cron_expression="*/5 * * * *",
            action_type="http",
            action_config={"method": "GET", "url": "https://old.example.com"},
        )
    )

    updated = service.update_job(
        str(job.id),
        JobUpdate(action_config={"method": "POST", "url": "https://new.example.com"}),
    )
    assert "https://new.example.com" in updated.runtime_spec["command"]
    assert "POST" in updated.runtime_spec["command"]
    assert "https://old.example.com" not in updated.runtime_spec["command"]
