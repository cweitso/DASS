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
