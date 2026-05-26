from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.job import Job
from app.models.task import Task
from app.services.worker_service import WorkerService
from app.queue.memory import MemoryQueueClient


def _job(db_session, **overrides):
    """Helper to create a test job."""
    job = Job(
        id=str(uuid4()),
        name=overrides.get("name", f"job-{uuid4()}"),
        cron_expression="* * * * *",
        action_type=overrides.get("action_type", "http"),
        action_config=overrides.get(
            "action_config",
            {"method": "GET", "url": "https://example.com", "timeout_seconds": 1},
        ),
        # S4: WorkerService._execute_job 需要有效的 runtime_spec 才能跑 ContainerSpec(**spec)
        runtime_spec=overrides.get(
            "runtime_spec",
            {"image": "alpine:3", "command": ["true"], "env": {}, "timeout_seconds": 1},
        ),
        enabled=overrides.get("enabled", True),
        concurrency_policy=overrides.get("concurrency_policy", "allow"),
        max_retries=overrides.get("max_retries", 0),
        next_fire_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(job)
    db_session.commit()
    return job


class TestWorkerService:
    """Tests for WorkerService task claiming, processing, and retry logic."""

    def test_worker_claims_pending_task_atomically(self, db_session):
        """Worker should atomically claim a pending task."""
        queue = MemoryQueueClient()
        job = _job(db_session)
        task = Task(job_id=job.id, status="pending", trigger_type="manual", retry_count=0)
        db_session.add(task)
        db_session.commit()
        service = WorkerService(db_session, queue, "worker-1")
        claimed = service.claim_task(str(task.id))
        assert claimed is not None
        assert claimed.status == "running"

    def test_worker_executes_http_action(self, db_session):
        """Worker should execute HTTP action and mark task as success."""
        queue = MemoryQueueClient()
        job = _job(db_session)
        task = Task(job_id=job.id, status="pending", trigger_type="manual", retry_count=0)
        db_session.add(task)
        db_session.commit()

        service = WorkerService(db_session, queue, "worker-1")

        class SuccessExecutor:
            def run(self, *args, **kwargs):
                from app.services.execution_service import ExecutionResult
                return ExecutionResult(success=True, stdout="ok", stderr="")

        service.executor = SuccessExecutor()
        assert service.process_task_id(str(task.id))
        updated = db_session.get(Task, task.id)
        assert updated.status == "success"

    def test_retry_flow(self, db_session):
        """Worker should create retry task when execution fails."""
        queue = MemoryQueueClient()
        job = _job(db_session, max_retries=1)
        task = Task(job_id=job.id, status="pending", trigger_type="manual", retry_count=0)
        db_session.add(task)
        db_session.commit()
        service = WorkerService(db_session, queue, "worker-1")

        class FailingExecutor:
            def run(self, *args, **kwargs):
                from app.services.execution_service import ExecutionResult
                return ExecutionResult(success=False, stdout="", stderr="boom")

        service.executor = FailingExecutor()
        service.process_task_id(str(task.id))
        tasks = db_session.query(Task).filter(Task.job_id == job.id).all()
        assert len(tasks) == 2
        
        # Verify the new task
        new_task = sorted(tasks, key=lambda t: t.retry_count)[1]
        assert new_task.status == "pending"
        assert new_task.retry_count == 1

    def test_no_retry_final_failure(self, db_session):
        """Worker should mark task as final failure without retries."""
        queue = MemoryQueueClient()
        job = _job(db_session, max_retries=0)
        task = Task(job_id=job.id, status="pending", trigger_type="manual", retry_count=0)
        db_session.add(task)
        db_session.commit()
        service = WorkerService(db_session, queue, "worker-1")

        class FailingExecutor:
            def run(self, *args, **kwargs):
                from app.services.execution_service import ExecutionResult
                return ExecutionResult(success=False, stdout="", stderr="boom")

        service.executor = FailingExecutor()
        service.process_task_id(str(task.id))
        tasks = db_session.query(Task).filter(Task.job_id == job.id).all()
        assert len(tasks) == 1
        assert tasks[0].status == "final_failed"

    def test_job_not_found(self, db_session):
        """Worker should handle task where job was deleted."""
        queue = MemoryQueueClient()
        job = _job(db_session)
        task = Task(job_id=job.id, status="pending", trigger_type="manual", retry_count=0)
        db_session.add(task)

        # Delete job
        db_session.delete(job)
        db_session.commit()

        service = WorkerService(db_session, queue, "worker-1")
        service.process_task_id(str(task.id))

        updated = db_session.get(Task, task.id)
        assert updated.status == "final_failed"
        assert updated.stderr == "Job not found"

    def test_heartbeat_extends_visibility_while_running(self, db_session, monkeypatch):
        """While the executor is running, the heartbeat thread should call extend_visibility."""
        import time
        from contextlib import contextmanager

        # 把 worker_service.SessionLocal mock 成 no-op，避免 heartbeat thread 撞到測試環境沒 postgres
        @contextmanager
        def fake_session():
            class _S:
                info: dict = {}
                def execute(self, *a, **kw): pass
                def commit(self): pass
            yield _S()
        monkeypatch.setattr("app.services.worker_service.SessionLocal", fake_session)

        queue = MemoryQueueClient()
        job = _job(db_session)
        task = Task(job_id=job.id, status="pending", trigger_type="manual", retry_count=0)
        db_session.add(task)
        db_session.commit()

        # claim_seconds=2 → heartbeat_interval=max(1, 1)=1s
        service = WorkerService(db_session, queue, "worker-1", claim_seconds=2)

        class SlowExecutor:
            def run(self, *args, **kwargs):
                from app.services.execution_service import ExecutionResult
                # 跑 ~2.5s 讓 heartbeat 至少 fire 1 次（interval=1s）
                time.sleep(2.5)
                return ExecutionResult(success=True, stdout="ok", stderr="")

        service.executor = SlowExecutor()

        calls: list[int] = []
        def extend(seconds: int) -> None:
            calls.append(seconds)

        service.process_task_id(str(task.id), extend_visibility=extend)

        assert len(calls) >= 1, "heartbeat should call extend_visibility at least once"
        assert all(s == 2 for s in calls), "extend_visibility called with claim_seconds"

    def test_claim_fail_running_task_does_not_delete_message(self, db_session):
        """Task 還在 running（被其他 worker 佔著），claim 失敗時應該回 False 留 message。"""
        queue = MemoryQueueClient()
        job = _job(db_session)
        # 任務已被 worker-other 佔住，且 lock 尚未過期
        task = Task(
            job_id=job.id,
            status="running",
            trigger_type="manual",
            retry_count=0,
            locked_by="worker-other",
            locked_until=datetime.now(UTC) + timedelta(seconds=60),
        )
        db_session.add(task)
        db_session.commit()

        # claim_seconds=1 → 5 retries × 0.5s 內 status 不變，仍無法 claim
        service = WorkerService(db_session, queue, "worker-1", claim_seconds=1)
        result = service.process_task_id(str(task.id))
        assert result is False, "running task 應該留 message 給 SQS 自然 surface"

    def test_claim_fail_terminal_task_deletes_message(self, db_session):
        """Task 已 success/final_failed，message 沒人會處理，回 True 刪除。"""
        queue = MemoryQueueClient()
        job = _job(db_session)
        task = Task(
            job_id=job.id,
            status="success",
            trigger_type="manual",
            retry_count=0,
        )
        db_session.add(task)
        db_session.commit()

        service = WorkerService(db_session, queue, "worker-1", claim_seconds=1)
        result = service.process_task_id(str(task.id))
        assert result is True, "terminal task 的 message 應刪除"

    def test_claim_fail_missing_task_deletes_message(self, db_session):
        """Task 不存在於 DB，message 是孤兒，回 True 刪除。"""
        queue = MemoryQueueClient()
        service = WorkerService(db_session, queue, "worker-1", claim_seconds=1)
        result = service.process_task_id(str(uuid4()))
        assert result is True, "缺失 task 的 message 應刪除"

    def test_no_heartbeat_when_callback_omitted(self, db_session):
        """When extend_visibility is None, the worker should still run successfully."""
        queue = MemoryQueueClient()
        job = _job(db_session)
        task = Task(job_id=job.id, status="pending", trigger_type="manual", retry_count=0)
        db_session.add(task)
        db_session.commit()

        service = WorkerService(db_session, queue, "worker-1")

        class SuccessExecutor:
            def run(self, *args, **kwargs):
                from app.services.execution_service import ExecutionResult
                return ExecutionResult(success=True, stdout="ok", stderr="")

        service.executor = SuccessExecutor()
        assert service.process_task_id(str(task.id))  # no extend_visibility kwarg
