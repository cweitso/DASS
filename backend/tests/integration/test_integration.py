"""Integration tests — cross-module end-to-end with real PostgreSQL + LocalStack SQS."""
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
        queue_name_normal=os.environ.get("DASS_QUEUE_NAME_NORMAL", "dass-tasks-normal"),
        queue_name_scheduled=os.environ.get("DASS_QUEUE_NAME_SCHEDULED", "dass-tasks-scheduled"),
        sqs_endpoint_url=os.environ.get("DASS_SQS_ENDPOINT_URL", "http://localhost:4566"),
        queue_backend="sqs",
    )
    # Scheduler 派發必須落 scheduled queue，normal queue 應保持空 — 對應 worker 端的 normal > scheduled > retry 優先序
    normal_sqs = SQSQueueClient(settings, queue_name=settings.queue_name_normal)
    scheduled_sqs = SQSQueueClient(settings, queue_name=settings.queue_name_scheduled)

    job = make_job(
        name="integ-dispatch",
        next_fire_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    main_db.flush()

    from sqlalchemy.orm import sessionmaker
    factory = sessionmaker(bind=main_db.get_bind())
    
    # 修改為只傳入 session_maker 與 scheduled_sqs
    service = SchedulerService(factory, scheduled_sqs)
    service.sync_jobs()
    
    dispatched = service.dispatch_due_jobs()
    assert dispatched >= 1

    scheduled_msgs = scheduled_sqs.receive_tasks(max_messages=1, wait_time_seconds=5)
    assert len(scheduled_msgs) >= 1
    normal_msgs = normal_sqs.receive_tasks(max_messages=1, wait_time_seconds=0)
    assert normal_msgs == []


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


def test_worker_executes_and_marks_result(main_db, make_job, make_task):
    from app.queue.memory import MemoryQueueClient
    from app.services.execution_service import ExecutionResult
    from app.services.worker_service import WorkerService

    job = make_job(name="integ-exec")
    task = make_task(job.id, status="pending")
    main_db.flush()

    queue = MemoryQueueClient()
    service = WorkerService(main_db, queue, "ci-worker")

    class SuccessExecutor:
        def run(self, *a, **kw):
            return ExecutionResult(success=True, stdout="ok", stderr="")

    service.executor = SuccessExecutor()
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
    from sqlalchemy.orm import sessionmaker
    factory = sessionmaker(bind=main_db.get_bind())
    service = SchedulerService(factory, queue)
    service.sync_jobs()
    
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

    monkeypatch.setattr("app.api.v1.jobs.get_normal_queue_client", lambda: MemoryQueueClient())
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
            assert any(j["id"] == job_id for j in resp.json()["items"])

            resp = client.delete(f"/api/v1/jobs/{job_id}")
            assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


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


def _scheduler_service(main_db):
    from sqlalchemy.orm import sessionmaker

    from app.core.config import Settings
    from app.queue.sqs import SQSQueueClient
    from app.services.scheduler_service import SchedulerService

    settings = Settings(
        queue_name_scheduled=os.environ.get("DASS_QUEUE_NAME_SCHEDULED", "dass-tasks-scheduled"),
        sqs_endpoint_url=os.environ.get("DASS_SQS_ENDPOINT_URL", "http://localhost:4566"),
        queue_backend="sqs",
    )
    scheduled_sqs = SQSQueueClient(settings, queue_name=settings.queue_name_scheduled)
    factory = sessionmaker(bind=main_db.get_bind())
    return SchedulerService(factory, scheduled_sqs), scheduled_sqs


def test_dependency_chaining_enqueues_downstream(main_db, make_job, make_task, purge_queues):
    """全部上游成功後，trigger_dependent_jobs 應建立下游 dependency task 並送進 scheduled queue (S4 DAG)."""
    upstream = make_job(name="integ-chain-up")
    downstream = make_job(name="integ-chain-down")
    upstream.downstream_jobs.append(downstream)
    make_task(upstream.id, status="success", trigger_type="scheduled")
    main_db.flush()

    service, scheduled_sqs = _scheduler_service(main_db)
    assert service.trigger_dependent_jobs() == 1

    main_db.expire_all()
    down_tasks = main_db.query(Task).filter(Task.job_id == downstream.id).all()
    assert len(down_tasks) == 1
    assert down_tasks[0].trigger_type == "dependency"
    assert down_tasks[0].status == "pending"

    up_task = main_db.query(Task).filter(Task.job_id == upstream.id).one()
    assert up_task.processed_for_chaining is True

    msgs = scheduled_sqs.receive_tasks(max_messages=1, wait_time_seconds=5)
    assert len(msgs) >= 1
    assert json.loads(msgs[0].body)["task_id"] == str(down_tasks[0].id)


def test_dependency_chaining_blocks_until_all_upstreams_succeed(main_db, make_job, make_task, purge_queues):
    """下游有兩個上游時，只要有一個上游還沒成功就不該被觸發 (S4 DAG fan-in)."""
    up_a = make_job(name="integ-chain-a")
    up_b = make_job(name="integ-chain-b")
    downstream = make_job(name="integ-chain-c")
    up_a.downstream_jobs.append(downstream)
    up_b.downstream_jobs.append(downstream)
    make_task(up_a.id, status="success", trigger_type="scheduled")  # 只有 A 成功，B 還沒跑
    main_db.flush()

    service, scheduled_sqs = _scheduler_service(main_db)
    assert service.trigger_dependent_jobs() == 0

    main_db.expire_all()
    assert main_db.query(Task).filter(Task.job_id == downstream.id).count() == 0
    assert scheduled_sqs.receive_tasks(max_messages=1, wait_time_seconds=1) == []


def _dag_has_cycle(jobs: list[Job]) -> bool:
    """以 DFS + 三色標記偵測 job_dependencies 有向圖是否含環。

    WHITE=未訪問 / GRAY=在當前遞迴路徑上 / BLACK=已完成。
    DFS 過程中若走到一個仍是 GRAY 的節點，代表遇到回邊 (back edge)，即有環。
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {str(j.id): WHITE for j in jobs}
    adjacency: dict[str, list[str]] = {
        str(j.id): [str(d.id) for d in j.downstream_jobs] for j in jobs
    }

    def visit(node: str) -> bool:
        color[node] = GRAY
        for nxt in adjacency.get(node, []):
            if color.get(nxt) == GRAY:
                return True  # 回邊 → 有環
            if color.get(nxt) == WHITE and visit(nxt):
                return True
        color[node] = BLACK
        return False

    return any(color[str(j.id)] == WHITE and visit(str(j.id)) for j in jobs)


def test_dag_cycle_detection_on_real_postgres(main_db, make_job):
    """job_dependencies 存進真實 PostgreSQL 後，能從持久化的邊判斷 DAG 有無 cycle。

    系統目前不在建立關聯時阻擋成環（runtime 靠 processed_for_chaining 防無限迴圈），
    這支測試守住的是：經過自關聯多對多 association table round-trip 後，圖的邊資訊
    完整無損，cycle 偵測能在無環鏈上回 False、在補成環後回 True。
    """
    a = make_job(name="integ-dag-a")
    b = make_job(name="integ-dag-b")
    c = make_job(name="integ-dag-c")

    # 先建一條無環鏈 A→B→C，判定應為「無環」
    a.downstream_jobs.append(b)
    b.downstream_jobs.append(c)
    main_db.flush()
    main_db.expire_all()  # 清掉 identity map 的關聯快取，強制重新從 DB 讀邊

    job_ids = [a.id, b.id, c.id]
    jobs = main_db.query(Job).filter(Job.id.in_(job_ids)).all()
    assert _dag_has_cycle(jobs) is False

    # 補一條 C→A 形成環 A→B→C→A，判定應為「有環」
    main_db.get(Job, c.id).downstream_jobs.append(main_db.get(Job, a.id))
    main_db.flush()
    main_db.expire_all()

    jobs = main_db.query(Job).filter(Job.id.in_(job_ids)).all()
    assert _dag_has_cycle(jobs) is True


# ── P0/P1: concurrency, end-to-end, leader election, scheduler lifecycle ─────


def _normal_sqs():
    from app.core.config import Settings
    from app.queue.sqs import SQSQueueClient

    settings = Settings(
        queue_name=os.environ.get("DASS_QUEUE_NAME_NORMAL", "dass-tasks-normal"),
        sqs_endpoint_url=os.environ.get("DASS_SQS_ENDPOINT_URL", "http://localhost:4566"),
        queue_backend="sqs",
    )
    return SQSQueueClient(settings)


def _override_db(main_db):
    def _gen():
        yield main_db

    return _gen


def test_concurrent_claim_only_one_worker_wins(main_engine, purge_queues):
    """兩個 worker 在獨立連線上同時搶同一個 pending task，只能一個成功 claim — 系統第一鐵律：不重複執行。"""
    import threading

    from sqlalchemy.orm import sessionmaker

    from app.queue.memory import MemoryQueueClient
    from app.services.worker_service import WorkerService

    Session = sessionmaker(bind=main_engine)
    # 用獨立連線把資料 commit 進真實 DB，兩個 worker 的連線才都看得到
    setup = Session()
    job = Job(
        id=str(uuid4()),
        name=f"integ-race-{uuid4().hex[:8]}",
        cron_expression="* * * * *",
        action_type="http",
        action_config={"method": "GET", "url": "https://example.com", "timeout_seconds": 1, "headers": {}},
        runtime_spec={"image": "alpine:3", "command": ["true"], "env": {}, "timeout_seconds": 1},
        enabled=True,
        concurrency_policy="allow",
        max_retries=0,
        next_fire_at=datetime.now(UTC),
    )
    task = Task(job_id=job.id, status="pending", trigger_type="scheduled", retry_count=0)
    setup.add(job)
    setup.add(task)
    setup.commit()
    task_id, job_id = str(task.id), str(job.id)
    setup.close()

    try:
        results: dict[str, object] = {}
        barrier = threading.Barrier(2)

        def race(name: str) -> None:
            db = Session()
            try:
                worker = WorkerService(db, MemoryQueueClient(), name)
                barrier.wait()
                results[name] = worker.claim_task(task_id)
            finally:
                db.close()

        threads = [threading.Thread(target=race, args=(f"w{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [name for name, claimed in results.items() if claimed is not None]
        assert len(winners) == 1, f"expected exactly one winner, got {winners}"

        check = Session()
        row = check.get(Task, task_id)
        assert row.status == "running"
        assert row.locked_by == winners[0]
        check.close()
    finally:
        cleanup = Session()
        cleanup.query(Task).filter(Task.job_id == job_id).delete()
        cleanup.query(Job).filter(Job.id == job_id).delete()
        cleanup.commit()
        cleanup.close()


def test_normal_job_end_to_end_via_api_and_worker(main_db, purge_queues, monkeypatch):
    """API 建立 no-cron job → 自動進 normal queue → worker 撈起執行 → task success（one-time job 全鏈路）。"""
    from fastapi.testclient import TestClient

    from app.api.deps import get_db
    from app.main import app
    from app.services.execution_service import ExecutionResult
    from app.services.worker_service import WorkerService

    normal_sqs = _normal_sqs()
    monkeypatch.setattr("app.services.job_service.get_normal_queue_client", lambda: normal_sqs)
    app.dependency_overrides[get_db] = _override_db(main_db)

    try:
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/jobs",
                json={
                    "name": f"integ-e2e-{uuid4().hex[:8]}",
                    "action_type": "http",
                    "action_config": {"method": "GET", "url": "https://example.com", "timeout_seconds": 5, "headers": {}},
                    "enabled": True,
                    "concurrency_policy": "allow",
                    "max_retries": 0,
                },
            )
            assert resp.status_code == 200
            assert resp.json()["cron_expression"] is None  # one-time job

        msgs = normal_sqs.receive_tasks(max_messages=1, wait_time_seconds=5)
        assert len(msgs) >= 1
        task_id = json.loads(msgs[0].body)["task_id"]

        worker = WorkerService(main_db, normal_sqs, "ci-worker")

        class SuccessExecutor:
            def run(self, *a, **kw):
                return ExecutionResult(success=True, stdout="ok", stderr="")

        worker.executor = SuccessExecutor()
        assert worker.process_task_id(task_id) is True

        main_db.expire_all()
        assert main_db.get(Task, task_id).status == "success"
    finally:
        app.dependency_overrides.clear()


def test_trigger_endpoint_enqueues_to_normal_queue(main_db, make_job, purge_queues, monkeypatch):
    """POST /jobs/{id}/trigger 必須把 task 送進 normal queue，trigger_type=manual。"""
    from fastapi.testclient import TestClient

    from app.api.deps import get_db
    from app.main import app

    normal_sqs = _normal_sqs()
    monkeypatch.setattr("app.api.v1.jobs.get_normal_queue_client", lambda: normal_sqs)
    app.dependency_overrides[get_db] = _override_db(main_db)

    job = make_job(name="integ-trigger")
    main_db.flush()
    try:
        with TestClient(app) as client:
            resp = client.post(f"/api/v1/jobs/{job.id}/trigger")
            assert resp.status_code == 200
            task_id = resp.json()["task_id"]
    finally:
        app.dependency_overrides.clear()

    msgs = normal_sqs.receive_tasks(max_messages=1, wait_time_seconds=5)
    assert task_id in [json.loads(m.body)["task_id"] for m in msgs]
    main_db.expire_all()
    assert main_db.get(Task, task_id).trigger_type == "manual"


def test_retry_endpoint_enqueues_to_retry_queue(main_db, make_job, make_task, purge_queues, monkeypatch):
    """POST /tasks/{id}/retry 必須建立 retry_count+1 的新 task 並送進 retry queue。"""
    from fastapi.testclient import TestClient

    from app.api.deps import get_db
    from app.core.config import Settings
    from app.main import app
    from app.queue.sqs import SQSQueueClient

    settings = Settings(
        sqs_endpoint_url=os.environ.get("DASS_SQS_ENDPOINT_URL", "http://localhost:4566"),
        queue_backend="sqs",
    )
    retry_sqs = SQSQueueClient(settings, queue_name=os.environ.get("DASS_QUEUE_NAME_RETRY", "dass-tasks-retry"))
    monkeypatch.setattr("app.api.v1.tasks.get_retry_queue_client", lambda: retry_sqs)
    app.dependency_overrides[get_db] = _override_db(main_db)

    job = make_job(name="integ-retry-endpoint", max_retries=2)
    task = make_task(job.id, status="failed", retry_count=0)
    main_db.flush()
    try:
        with TestClient(app) as client:
            resp = client.post(f"/api/v1/tasks/{task.id}/retry")
            assert resp.status_code == 200
            retry_task_id = resp.json()["retry_task_id"]
    finally:
        app.dependency_overrides.clear()

    msgs = retry_sqs.receive_tasks(max_messages=1, wait_time_seconds=5)
    assert retry_task_id in [json.loads(m.body)["task_id"] for m in msgs]
    main_db.expire_all()
    assert main_db.get(Task, retry_task_id).retry_count == 1


def test_leader_election_advisory_lock_is_exclusive(main_engine):
    """pg_try_advisory_lock：第一條連線拿到鎖，第二條同 key 拿不到——scheduler leader election 的基石。"""
    from sqlalchemy import text

    LOCK_KEY = 114514  # 與 cli.run_scheduler 的 LOCK_KEY 一致
    c1 = main_engine.connect()
    c2 = main_engine.connect()
    try:
        assert c1.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar() is True
        assert c2.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar() is False
        c1.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
        c1.commit()
        assert c2.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar() is True
        c2.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
        c2.commit()
    finally:
        c1.close()
        c2.close()


def test_sync_jobs_evicts_disabled_job(main_db, make_job, purge_queues):
    """停用的 scheduled job 經 sync_jobs 後要從 cache 逐出，不再被 dispatch（tombstone）。"""
    service, scheduled_sqs = _scheduler_service(main_db)
    job = make_job(name="integ-tombstone", next_fire_at=datetime.now(UTC) - timedelta(seconds=10))
    main_db.flush()
    service.sync_jobs()

    job.enabled = False
    main_db.flush()
    service.last_sync_at = None  # 強制全量重掃，跳過 updated_at 增量游標的時鐘誤差
    service.sync_jobs()

    assert service.dispatch_due_jobs() == 0
    assert scheduled_sqs.receive_tasks(max_messages=1, wait_time_seconds=1) == []


def test_dispatch_forbid_skips_when_running_but_advances_next_fire_at(main_db, make_job, make_task, purge_queues):
    """concurrency_policy=forbid 且已有 running task 時不派發，但 next_fire_at 仍要前進。"""
    service, scheduled_sqs = _scheduler_service(main_db)
    job = make_job(
        name="integ-forbid",
        concurrency_policy="forbid",
        next_fire_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    make_task(job.id, status="running")
    main_db.flush()
    service.sync_jobs()

    assert service.dispatch_due_jobs() == 0
    assert scheduled_sqs.receive_tasks(max_messages=1, wait_time_seconds=1) == []

    main_db.expire_all()
    main_db.refresh(job)
    assert job.next_fire_at > datetime.now(UTC)


def test_dispatch_advances_next_fire_at_and_does_not_double_fire(main_db, make_job, purge_queues):
    """派發後 next_fire_at 前進到下個 cron tick；同一輪再 dispatch 不會重複發射。"""
    service, scheduled_sqs = _scheduler_service(main_db)
    job = make_job(name="integ-advance", next_fire_at=datetime.now(UTC) - timedelta(seconds=10))
    main_db.flush()
    service.sync_jobs()

    assert service.dispatch_due_jobs() >= 1
    main_db.expire_all()
    main_db.refresh(job)
    assert job.next_fire_at > datetime.now(UTC)
    assert service.dispatch_due_jobs() == 0
