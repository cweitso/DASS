"""End-to-end: a task's runtime_spec is executed as a real container.

Needs a working Docker daemon — the worker shells out to `docker run`.
"""
from __future__ import annotations

import pytest

from app.models.job import Job
from app.models.task import Task
from app.queue.memory import MemoryQueueClient
from app.services.worker_service import WorkerService
from app.utils.time import utcnow

pytestmark = pytest.mark.integration


def test_worker_runs_the_container_described_by_runtime_spec(db_session):
    job = Job(
        name="test-worker-e2e-job",
        cron_expression="* * * * *",
        action_type="shell",
        action_config={},
        runtime_spec={
            "image": "alpine:latest",
            "command": ["echo", "worker testing container spec execution!"],
            "env": {"TEST_VAR": "HELLO_WORKER"},
            "timeout_seconds": 30,
            "cpu": 0.5,
            "memory_mb": 128,
        },
        concurrency_policy="allow",
        max_retries=0,
        next_fire_at=utcnow(),
        enabled=True,
    )
    db_session.add(job)
    db_session.commit()

    task = Task(job_id=job.id, status="pending", trigger_type="manual")
    db_session.add(task)
    db_session.commit()

    worker = WorkerService(
        db=db_session,
        queue_client=MemoryQueueClient(),
        worker_id="test-e2e-worker",
    )
    assert worker.process_task_id(str(task.id)) is True

    db_session.expire_all()
    finished = db_session.get(Task, task.id)
    assert finished.status == "success"
    assert "worker testing container spec execution!" in finished.stdout
    assert finished.started_at is not None
    assert finished.finished_at is not None
