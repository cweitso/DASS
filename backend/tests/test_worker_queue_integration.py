from __future__ import annotations

import json
import subprocess
from uuid import uuid4

import pytest

from app.models.job import Job
from app.models.task import Task
from app.queue.memory import MemoryQueueClient
from app.services.worker_service import WorkerService
from app.utils.time import utcnow


def _require_docker() -> None:
    result = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Docker daemon is required for worker container integration tests")


def _seed_container_task(db_session, output: str) -> Task:
    job = Job(
        name=f"worker-queue-integration-{uuid4()}",
        cron_expression="* * * * *",
        action_type="container",
        action_config={},
        runtime_spec={
            "image": "alpine:latest",
            "command": ["sh", "-c", f"echo {output}"],
            "timeout_seconds": 30,
        },
        enabled=True,
        concurrency_policy="allow",
        max_retries=0,
        next_fire_at=utcnow(),
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    task = Task(
        job_id=job.id,
        status="pending",
        trigger_type="manual",
        retry_count=0,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def _process_queue_messages(db_session, queue_client: MemoryQueueClient, max_messages: int) -> list[str]:
    worker = WorkerService(
        db=db_session,
        queue_client=queue_client,
        worker_id="worker-vm-integration-test",
    )

    messages = queue_client.receive_tasks(max_messages=max_messages, wait_time_seconds=0)
    processed_task_ids: list[str] = []

    for message in messages:
        task_id = json.loads(message.body)["task_id"]
        assert worker.process_task_id(task_id) is True
        queue_client.delete_message(message.receipt_handle)
        processed_task_ids.append(task_id)

    return processed_task_ids


def test_worker_vm_takes_one_task_id_from_queue_and_runs_container(db_session):
    _require_docker()
    queue_client = MemoryQueueClient()
    task = _seed_container_task(db_session, "worker-one-task-ok")
    queue_client.send_task(str(task.id))

    processed_task_ids = _process_queue_messages(db_session, queue_client, max_messages=1)

    assert processed_task_ids == [str(task.id)]

    db_session.expire_all()
    finished_task = db_session.get(Task, task.id)
    assert finished_task.status == "success"
    assert "worker-one-task-ok" in finished_task.stdout
    assert finished_task.locked_by is None
    assert finished_task.locked_until is None
    assert finished_task.started_at is not None
    assert finished_task.finished_at is not None


def test_worker_vm_takes_two_task_ids_from_queue_and_runs_containers(db_session):
    _require_docker()
    queue_client = MemoryQueueClient()
    first_task = _seed_container_task(db_session, "worker-two-task-first-ok")
    second_task = _seed_container_task(db_session, "worker-two-task-second-ok")
    queue_client.send_task(str(first_task.id))
    queue_client.send_task(str(second_task.id))

    processed_task_ids = _process_queue_messages(db_session, queue_client, max_messages=2)

    assert processed_task_ids == [str(first_task.id), str(second_task.id)]

    db_session.expire_all()
    finished_first_task = db_session.get(Task, first_task.id)
    finished_second_task = db_session.get(Task, second_task.id)

    assert finished_first_task.status == "success"
    assert "worker-two-task-first-ok" in finished_first_task.stdout
    assert finished_first_task.locked_by is None
    assert finished_first_task.locked_until is None
    assert finished_first_task.started_at is not None
    assert finished_first_task.finished_at is not None

    assert finished_second_task.status == "success"
    assert "worker-two-task-second-ok" in finished_second_task.stdout
    assert finished_second_task.locked_by is None
    assert finished_second_task.locked_until is None
    assert finished_second_task.started_at is not None
    assert finished_second_task.finished_at is not None
