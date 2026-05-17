"""
Sprint 4 integration tests — cross-module end-to-end with real PostgreSQL + LocalStack SQS.

xfail strategy: S4 features are marked @pytest.mark.xfail(strict=False).
XFAIL → XPASS automatically as feature PRs land; remove @xfail once stable.

Scenarios:
 1-8. Sprint 3 baseline — expected to pass directly
 9.   Dual-write: API create_job → Scheduler DB  (S4-SC-01, xfail)
10.   Multi-queue: retry enqueues to Retry Queue (S4-QUEUE-02, xfail)
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.job import Job
from app.models.task import Task


pytestmark = pytest.mark.integration


def test_repository_crud_on_real_postgres(main_db, make_job, make_task):
    from app.repositories.job_repository import JobRepository
    from app.repositories.task_repository import TaskRepository

    job_repo = JobRepository(main_db)
    task_repo = TaskRepository(main_db)

    job = make_job(name="integ-repo-crud")
    assert job.id is not None

    fetched = job_repo.get(job.id)
    assert fetched is not None
    assert fetched.name == "integ-repo-crud"

    task = make_task(job.id, status="pending")
    fetched_task = task_repo.get(str(task.id))
    assert fetched_task is not None

    tasks = task_repo.list_by_job(job.id)
    assert len(tasks) >= 1


def test_sqs_send_receive_delete(purge_queues):
    from app.core.config import Settings
    from app.queue.sqs import SQSQueueClient

    settings = Settings(
        queue_name=os.environ.get("DASS_QUEUE_NAME", "dass-tasks"),
        sqs_endpoint_url=os.environ.get("DASS_SQS_ENDPOINT_URL", "http://localhost:4566"),
        queue_backend="sqs",
    )
    client = SQSQueueClient(settings)

    task_id = str(uuid4())
    client.send_task(task_id)

    messages = client.receive_tasks(max_messages=1, wait_time_seconds=5)
    assert len(messages) >= 1

    body = json.loads(messages[0].body)
    assert body["task_id"] == task_id

    client.delete_message(messages[0].receipt_handle)

    messages2 = client.receive_tasks(max_messages=1, wait_time_seconds=1)
    task_ids = [json.loads(m.body).get("task_id") for m in messages2]
    assert task_id not in task_ids


def test_scheduler_dispatch_enqueues_to_sqs(main_db, make_job, purge_queues):
    from app.core.config import Settings
    from app.queue.sqs import SQSQueueClient
    from app.services.scheduler_service import SchedulerService

    settings = Settings(
        queue_name=os.environ.get("DASS_QUEUE_NAME", "dass-tasks"),
        sqs_endpoint_url=os.environ.get("DASS_SQS_ENDPOINT_URL", "http://localhost:4566"),
        queue_backend="sqs",
    )
    sqs = SQSQueueClient(settings)

    job = make_job(
        name="integ-dispatch",
        next_fire_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    main_db.flush()

    service = SchedulerService(main_db, sqs)
    dispatched = service.dispatch_due_jobs()
    assert dispatched >= 1

    messages = sqs.receive_tasks(max_messages=1, wait_time_seconds=5)
    assert len(messages) >= 1


def test_worker_atomic_claim_on_postgres(main_db, make_job, make_task):
    # w1 and w2 share the same session, so this tests application-level
    # WHERE status='pending' filtering (sequential), not true concurrent PG
    # row-level locking. A true concurrency test requires separate DB connections.
    from app.queue.memory import MemoryQueueClient
    from app.services.worker_service import WorkerService

    job = make_job(name="integ-claim")
    task = make_task(job.id, status="pending")
    main_db.flush()

    queue = MemoryQueueClient()
    w1 = WorkerService(main_db, queue, "worker-1")
    w2 = WorkerService(main_db, queue, "worker-2")

    claimed1 = w1.claim_task(str(task.id))
    claimed2 = w2.claim_task(str(task.id))

    results = [claimed1, claimed2]
    assert sum(1 for r in results if r is not None) == 1


def test_worker_executes_and_marks_result(main_db, make_job, make_task, monkeypatch):
    from app.queue.memory import MemoryQueueClient
    from app.services.worker_service import WorkerService

    job = make_job(
        name="integ-exec",
        action_type="http",
        action_config={
            "method": "GET",
            "url": "https://httpbin.org/get",
            "timeout_seconds": 5,
            "headers": {},
        },
    )
    task = make_task(job.id, status="pending")
    main_db.flush()

    # mock HTTP to avoid external dependency
    class DummyResponse:
        is_success = True
        status_code = 200
        text = "ok"

    class DummyClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, *a, **kw):
            return DummyResponse()

    monkeypatch.setattr(
        "app.services.execution_service.httpx.Client", lambda timeout: DummyClient()
    )

    queue = MemoryQueueClient()
    service = WorkerService(main_db, queue, "ci-worker")
    result = service.process_task_id(str(task.id))

    assert result is True
    updated = main_db.get(Task, task.id)
    assert updated.status == "success"


def test_retry_creates_new_task_and_enqueues(main_db, make_job, make_task):
    from app.queue.memory import MemoryQueueClient
    from app.services.execution_service import ExecutionResult
    from app.services.worker_service import WorkerService

    job = make_job(name="integ-retry", max_retries=2)
    task = make_task(job.id, status="pending", retry_count=0)
    main_db.flush()

    queue = MemoryQueueClient()
    service = WorkerService(main_db, queue, "ci-worker")

    class FailExecutor:
        def run(self, *a, **kw):
            return ExecutionResult(success=False, stdout="", stderr="boom")

    service.executor = FailExecutor()
    service.process_task_id(str(task.id))

    tasks = main_db.query(Task).filter(Task.job_id == job.id).all()
    assert len(tasks) == 2
    retry_task = [t for t in tasks if t.retry_count == 1]
    assert len(retry_task) == 1
    assert retry_task[0].status == "pending"


def test_orphan_recovery_on_real_postgres(main_db, make_job, purge_queues):
    from app.queue.memory import MemoryQueueClient
    from app.services.scheduler_service import SchedulerService

    job = make_job(name="integ-orphan")
    task = Task(
        job_id=job.id,
        status="running",
        trigger_type="scheduled",
        retry_count=0,
        locked_by="dead-worker",
        locked_until=datetime.now(UTC) - timedelta(seconds=60),
    )
    main_db.add(task)
    main_db.flush()

    queue = MemoryQueueClient()
    service = SchedulerService(main_db, queue)
    recovered = service.recover_orphans()

    assert recovered >= 1
    main_db.refresh(task)
    assert task.status == "pending"
    assert task.locked_by is None


def test_api_job_crud_on_postgres(main_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.deps import get_db
    from app.main import app
    from app.queue.memory import MemoryQueueClient

    def override_get_db():
        try:
            yield main_db
        finally:
            pass

    monkeypatch.setattr("app.api.v1.jobs.get_queue_client", lambda: MemoryQueueClient())
    monkeypatch.setattr("app.api.v1.tasks.get_retry_queue_client", lambda: MemoryQueueClient())
    app.dependency_overrides[get_db] = override_get_db

    try:
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/jobs",
                json={
                    "name": f"integ-api-crud-{uuid4().hex[:8]}",
                    "cron_expression": "*/5 * * * *",
                    "action_type": "http",
                    "action_config": {
                        "method": "GET",
                        "url": "https://example.com",
                        "timeout_seconds": 5,
                        "headers": {},
                    },
                    "enabled": True,
                    "concurrency_policy": "allow",
                    "max_retries": 0,
                },
            )
            assert resp.status_code == 200
            job_id = resp.json()["id"]

            resp = client.get(f"/api/v1/jobs/{job_id}")
            assert resp.status_code == 200

            resp = client.get("/api/v1/jobs")
            assert resp.status_code == 200
            assert any(j["id"] == job_id for j in resp.json())

            resp = client.delete(f"/api/v1/jobs/{job_id}")
            assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


@pytest.mark.xfail(reason="S4-SC-01 dual-write not implemented", strict=False)
def test_dual_write_api_to_scheduler_db(main_db, scheduler_db, monkeypatch):
    """API create_job must write to both main DB and Scheduler DB (S4-SC-01)."""
    from fastapi.testclient import TestClient

    from app.api.deps import get_db
    from app.main import app
    from app.queue.memory import MemoryQueueClient

    def override_get_db():
        try:
            yield main_db
        finally:
            pass

    monkeypatch.setattr("app.api.v1.jobs.get_queue_client", lambda: MemoryQueueClient())
    monkeypatch.setattr("app.api.v1.tasks.get_retry_queue_client", lambda: MemoryQueueClient())
    app.dependency_overrides[get_db] = override_get_db

    job_name = f"integ-dual-write-{uuid4().hex[:8]}"

    try:
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/jobs",
                json={
                    "name": job_name,
                    "cron_expression": "*/10 * * * *",
                    "action_type": "http",
                    "action_config": {
                        "method": "GET",
                        "url": "https://example.com",
                        "timeout_seconds": 5,
                        "headers": {},
                    },
                    "enabled": True,
                    "concurrency_policy": "allow",
                    "max_retries": 0,
                },
            )
            assert resp.status_code == 200
            job_id = resp.json()["id"]
    finally:
        app.dependency_overrides.clear()

    main_job = main_db.get(Job, job_id)
    assert main_job is not None
    assert main_job.name == job_name

    scheduler_job = scheduler_db.get(Job, job_id)
    assert scheduler_job is not None, "dual-write did not persist job to Scheduler DB"
    assert scheduler_job.name == job_name


def test_retry_enqueues_to_retry_queue(main_db, make_job, make_task, purge_queues, sqs_client):
    """On failure, retry task must go to Retry Queue, not Normal Queue (S4-QUEUE-02)."""
    from app.core.config import Settings
    from app.queue.sqs import SQSQueueClient
    from app.services.execution_service import ExecutionResult
    from app.services.worker_service import WorkerService

    settings = Settings(
        queue_name=os.environ.get("DASS_QUEUE_NAME_NORMAL", "dass-tasks-normal"),
        sqs_endpoint_url=os.environ.get("DASS_SQS_ENDPOINT_URL", "http://localhost:4566"),
        queue_backend="sqs",
    )
    normal_sqs = SQSQueueClient(settings)
    retry_sqs = SQSQueueClient(
        settings,
        queue_name=os.environ.get("DASS_QUEUE_NAME_RETRY", "dass-tasks-retry"),
    )

    job = make_job(name="integ-multi-queue", max_retries=2)
    task = make_task(job.id, status="pending", retry_count=0)
    main_db.flush()

    service = WorkerService(main_db, normal_sqs, "ci-worker", retry_queue=retry_sqs)

    class FailExecutor:
        def run(self, *a, **kw):
            return ExecutionResult(success=False, stdout="", stderr="retry-test")

    service.executor = FailExecutor()
    service.process_task_id(str(task.id))

    retry_queue_name = os.environ.get("DASS_QUEUE_NAME_RETRY", "dass-tasks-retry")
    retry_url = sqs_client.get_queue_url(QueueName=retry_queue_name)["QueueUrl"]
    resp = sqs_client.receive_message(
        QueueUrl=retry_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=5,
    )
    messages = resp.get("Messages", [])
    assert len(messages) >= 1, "retry task not found in Retry Queue"

    body = json.loads(messages[0]["Body"])
    assert "task_id" in body

    normal_queue_name = os.environ.get("DASS_QUEUE_NAME_NORMAL", "dass-tasks-normal")
    normal_url = sqs_client.get_queue_url(QueueName=normal_queue_name)["QueueUrl"]
    resp2 = sqs_client.receive_message(
        QueueUrl=normal_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=1,
    )
    normal_messages = resp2.get("Messages", [])
    normal_task_ids = [json.loads(m["Body"]).get("task_id") for m in normal_messages]
    assert body["task_id"] not in normal_task_ids, "retry task must not appear in Normal Queue"
