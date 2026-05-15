"""
Sprint 4 整合測試 — 跨模組端對端驗證 (真實 PostgreSQL + LocalStack SQS)。

=== xfail 漸進策略 ===
每個 test 標注 @pytest.mark.xfail(reason="S4-XXX 未實作", strict=False)。

- strict=False → 測試意外通過時不 break CI，安靜變成 XPASS
- 組員 merge PR 實作了對應功能 → xfail 自動從 XFAIL 變 XPASS
- integration-ci.yml 的 Summary 表追蹤 XFAIL 數字倒數到零 = Sprint 4 完成
- 當你確認某功能已穩定通過，移除該 test 的 @xfail decorator 即可

=== Scenario 對照表 ===
 1. Repository CRUD on real PostgreSQL         (Sprint 3 已通過 ← 驗證基線)
 2. SQS send → receive → delete round-trip     (Sprint 3 已通過 ← 驗證基線)
 3. Scheduler dispatch → Task 進入 SQS          (Sprint 3 已通過 ← 驗證基線)
 4. Worker atomic claim on PostgreSQL           (Sprint 3 已通過 ← 驗證基線)
 5. Worker execute → mark result                (Sprint 3 已通過 ← 驗證基線)
 6. Retry flow → new Task + enqueue             (Sprint 3 已通過 ← 驗證基線)
 7. Orphan recovery on real PostgreSQL          (Sprint 3 已通過 ← 驗證基線)
 8. API Job CRUD on PostgreSQL                  (Sprint 3 已通過 ← 驗證基線)
 9. Dual-write: API create_job → Scheduler DB   (S4-SC-01 未實作)
10. Multi-Queue: retry enqueues to Retry Queue  (S4-QUEUE-02 未實作)
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


# ══════════════════════════════════════════════════════════
# Scenario 1: Repository CRUD on real PostgreSQL
# Sprint 3 基線 — 應直接通過
# ══════════════════════════════════════════════════════════
def test_repository_crud_on_real_postgres(main_db, make_job, make_task):
    """JobRepo.create → TaskRepo.create → get → list_by_job 在真實 PG 上驗證。"""
    from app.repositories.job_repository import JobRepository
    from app.repositories.task_repository import TaskRepository

    job_repo = JobRepository(main_db)
    task_repo = TaskRepository(main_db)

    # 建立 Job
    job = make_job(name="integ-repo-crud")
    assert job.id is not None

    # repo.get 取回
    fetched = job_repo.get(job.id)
    assert fetched is not None
    assert fetched.name == "integ-repo-crud"

    # 建立 Task
    task = make_task(job.id, status="pending")
    fetched_task = task_repo.get(str(task.id))
    assert fetched_task is not None

    # list_by_job
    tasks = task_repo.list_by_job(job.id)
    assert len(tasks) >= 1


# ══════════════════════════════════════════════════════════
# Scenario 2: SQS send → receive → delete (真實 LocalStack)
# Sprint 3 基線 — 應直接通過
# ══════════════════════════════════════════════════════════
def test_sqs_send_receive_delete(purge_queues):
    """SQSQueueClient: send_task → receive_tasks → delete_message round-trip。"""
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

    # delete
    client.delete_message(messages[0].receipt_handle)

    # 確認已刪除 — 再收不到同一筆
    messages2 = client.receive_tasks(max_messages=1, wait_time_seconds=1)
    task_ids = [json.loads(m.body).get("task_id") for m in messages2]
    assert task_id not in task_ids


# ══════════════════════════════════════════════════════════
# Scenario 3: Scheduler dispatch_due_jobs → Task 進入 SQS
# Sprint 3 基線 — 應直接通過
# ══════════════════════════════════════════════════════════
def test_scheduler_dispatch_enqueues_to_sqs(main_db, make_job, purge_queues):
    """SchedulerService.dispatch_due_jobs 在真實 PG+SQS 下建立 Task 並 enqueue。"""
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

    # 確認 SQS 收到
    messages = sqs.receive_tasks(max_messages=1, wait_time_seconds=5)
    assert len(messages) >= 1


# ══════════════════════════════════════════════════════════
# Scenario 4: Worker atomic claim on PostgreSQL
# Sprint 3 基線 — 應直接通過
# ══════════════════════════════════════════════════════════
def test_worker_atomic_claim_on_postgres(main_db, make_job, make_task):
    """兩個 WorkerService 同時 claim 同一 Task，只有一個成功 (PG row-level lock)。"""
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

    # 只有一個成功
    results = [claimed1, claimed2]
    assert sum(1 for r in results if r is not None) == 1


# ══════════════════════════════════════════════════════════
# Scenario 5: Worker execute → mark result
# Sprint 3 基線 — 應直接通過 (mock HTTP)
# ══════════════════════════════════════════════════════════
def test_worker_executes_and_marks_result(main_db, make_job, make_task, monkeypatch):
    """Worker claim → execute HTTP → mark_success 完整流程。"""
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

    # Mock HTTP 呼叫避免外部依賴
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


# ══════════════════════════════════════════════════════════
# Scenario 6: Retry flow → new Task + enqueue
# Sprint 3 基線 — 應直接通過
# ══════════════════════════════════════════════════════════
def test_retry_creates_new_task_and_enqueues(main_db, make_job, make_task):
    """失敗的 Task → _handle_failure → 建立 retry Task + enqueue。"""
    from app.queue.memory import MemoryQueueClient
    from app.services.execution_service import ExecutionResult
    from app.services.worker_service import WorkerService

    job = make_job(name="integ-retry", max_retries=2)
    task = make_task(job.id, status="pending", retry_count=0)
    main_db.flush()

    queue = MemoryQueueClient()
    service = WorkerService(main_db, queue, "ci-worker")

    # 強制失敗
    class FailExecutor:
        def run(self, *a, **kw):
            return ExecutionResult(success=False, stdout="", stderr="boom")

    service.executor = FailExecutor()
    service.process_task_id(str(task.id))

    # 應產生 retry task (retry_count=1)
    tasks = main_db.query(Task).filter(Task.job_id == job.id).all()
    assert len(tasks) == 2
    retry_task = [t for t in tasks if t.retry_count == 1]
    assert len(retry_task) == 1
    assert retry_task[0].status == "pending"


# ══════════════════════════════════════════════════════════
# Scenario 7: Orphan recovery on real PostgreSQL
# Sprint 3 基線 — 應直接通過
# ══════════════════════════════════════════════════════════
def test_orphan_recovery_on_real_postgres(main_db, make_job, purge_queues):
    """locked_until 過期的 running Task 被 recover_orphans 重設為 pending。"""
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


# ══════════════════════════════════════════════════════════
# Scenario 8: API Job CRUD on PostgreSQL
# Sprint 3 基線 — 應直接通過
# ══════════════════════════════════════════════════════════
def test_api_job_crud_on_postgres(main_db, monkeypatch):
    """POST /api/v1/jobs → GET → PUT → DELETE 完整 API 在真實 PG 上驗證。"""
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
    monkeypatch.setattr("app.api.v1.tasks.get_queue_client", lambda: MemoryQueueClient())
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        # Create
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

        # Get
        resp = client.get(f"/api/v1/jobs/{job_id}")
        assert resp.status_code == 200

        # List
        resp = client.get("/api/v1/jobs")
        assert resp.status_code == 200
        assert any(j["id"] == job_id for j in resp.json())

        # Delete
        resp = client.delete(f"/api/v1/jobs/{job_id}")
        assert resp.status_code == 200

    app.dependency_overrides.clear()


# ══════════════════════════════════════════════════════════
# Scenario 9: Dual-Write — API create_job 同時寫入 Scheduler DB
# Sprint 4 新功能 (S4-SC-01) — 預期 XFAIL
# ══════════════════════════════════════════════════════════
@pytest.mark.xfail(reason="S4-SC-01 Dual-Write 未實作", strict=False)
def test_dual_write_api_to_scheduler_db(main_db, scheduler_db, monkeypatch):
    """API create_job 後，同一筆 Job 應同時存在於主庫和 Scheduler DB。

    Sprint 4 的 Dual-Write 機制：
    API Server 收到 create_job 時，除了寫入主庫，
    也同時寫入 Scheduler 的 Local DB，
    讓 Scheduler 能從自己的 DB 讀取要派發的 Job。
    """
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
    monkeypatch.setattr("app.api.v1.tasks.get_queue_client", lambda: MemoryQueueClient())
    app.dependency_overrides[get_db] = override_get_db

    job_name = f"integ-dual-write-{uuid4().hex[:8]}"

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

    app.dependency_overrides.clear()

    # 主庫應有此 Job
    main_job = main_db.get(Job, job_id)
    assert main_job is not None
    assert main_job.name == job_name

    # Scheduler DB 也應有此 Job（Dual-Write 的核心驗證）
    scheduler_job = scheduler_db.get(Job, job_id)
    assert scheduler_job is not None, "Dual-Write 未將 Job 寫入 Scheduler DB"
    assert scheduler_job.name == job_name


# ══════════════════════════════════════════════════════════
# Scenario 10: Multi-Queue — retry 送到 Retry Queue
# Sprint 4 新功能 (S4-QUEUE-02) — 預期 XFAIL
# ══════════════════════════════════════════════════════════
@pytest.mark.xfail(reason="S4-QUEUE-02 Multi-Queue routing 未實作", strict=False)
def test_retry_enqueues_to_retry_queue(main_db, make_job, make_task, purge_queues, sqs_client):
    """失敗的 Task retry 應送到 Retry Queue 而非 Normal Queue。

    Sprint 4 的 Multi-Queue 機制：
    - 首次 dispatch → Normal Queue (dass-tasks-normal)
    - _handle_failure retry → Retry Queue (dass-tasks-retry)
    - worker-normal 監聽 Normal Queue
    - worker-retry 監聽 Retry Queue
    """
    from app.core.config import Settings
    from app.queue.sqs import SQSQueueClient
    from app.services.execution_service import ExecutionResult
    from app.services.worker_service import WorkerService

    # 建立用 Normal Queue 的 SQS client
    settings = Settings(
        queue_name=os.environ.get("DASS_QUEUE_NAME_NORMAL", "dass-tasks-normal"),
        sqs_endpoint_url=os.environ.get("DASS_SQS_ENDPOINT_URL", "http://localhost:4566"),
        queue_backend="sqs",
    )
    normal_sqs = SQSQueueClient(settings)

    job = make_job(name="integ-multi-queue", max_retries=2)
    task = make_task(job.id, status="pending", retry_count=0)
    main_db.flush()

    service = WorkerService(main_db, normal_sqs, "ci-worker")

    # 強制失敗
    class FailExecutor:
        def run(self, *a, **kw):
            return ExecutionResult(success=False, stdout="", stderr="retry-test")

    service.executor = FailExecutor()
    service.process_task_id(str(task.id))

    # 驗證：retry task 應送到 Retry Queue
    retry_queue_name = os.environ.get("DASS_QUEUE_NAME_RETRY", "dass-tasks-retry")
    retry_url = sqs_client.get_queue_url(QueueName=retry_queue_name)["QueueUrl"]
    resp = sqs_client.receive_message(
        QueueUrl=retry_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=5,
    )
    messages = resp.get("Messages", [])
    assert len(messages) >= 1, "Retry task 未送到 Retry Queue"

    body = json.loads(messages[0]["Body"])
    assert "task_id" in body

    # 且 Normal Queue 不應該有 retry task
    normal_queue_name = os.environ.get("DASS_QUEUE_NAME_NORMAL", "dass-tasks-normal")
    normal_url = sqs_client.get_queue_url(QueueName=normal_queue_name)["QueueUrl"]
    resp2 = sqs_client.receive_message(
        QueueUrl=normal_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=1,
    )
    normal_messages = resp2.get("Messages", [])
    normal_task_ids = [json.loads(m["Body"]).get("task_id") for m in normal_messages]
    assert body["task_id"] not in normal_task_ids, "Retry task 不應出現在 Normal Queue"
