"""Worker-VM behaviour: the admin API can start several workers, and one worker
runs container tasks back to back.

Needs a working Docker daemon.
"""
from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.models.job import Job
from app.models.task import Task
from app.queue.memory import MemoryQueueClient
from app.services.worker_service import WorkerService
from app.utils.time import utcnow

pytestmark = pytest.mark.integration


@pytest.fixture
def vm_admin_api_enabled():
    """POST /vms is disabled by default; turn it on for this test only."""
    settings = get_settings()
    original = settings.vm_admin_api_enabled
    settings.vm_admin_api_enabled = True
    yield
    settings.vm_admin_api_enabled = original


def test_api_creates_several_worker_vms_at_once(client, vm_admin_api_enabled):
    response = client.post("/vms", json={"count": 3, "instance_type": "t3.medium"})

    assert response.status_code == 200
    assert len(response.json()["vm_ids"]) == 3


def test_vms_endpoint_is_refused_when_disabled(client):
    assert client.post("/vms", json={"count": 1}).status_code == 403


def _seed_container_task(db_session, label: str) -> str:
    job = Job(
        name=f"job-for-{label}",
        cron_expression="* * * * *",
        action_type="shell",
        action_config={},
        runtime_spec={
            "image": "alpine:latest",
            "command": ["echo", f"Container {label} is running on this VM!"],
            "timeout_seconds": 30,
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
    return str(task.id)


def test_one_worker_runs_several_container_tasks(db_session):
    worker = WorkerService(
        db=db_session,
        queue_client=MemoryQueueClient(),
        worker_id="i-abcd1234_worker",
    )

    labels = ["Number-1", "Number-2", "Number-3"]
    task_ids = [_seed_container_task(db_session, label) for label in labels]

    for task_id in task_ids:
        assert worker.process_task_id(task_id) is True

    db_session.expire_all()
    for label, task_id in zip(labels, task_ids):
        finished = db_session.get(Task, task_id)
        assert finished.status == "success"
        assert f"Container {label} is running" in finished.stdout
