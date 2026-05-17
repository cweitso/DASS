from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.task import Task
from app.repositories.job_repository import JobRepository
from app.repositories.task_repository import TaskRepository
from app.services.execution_service import ExecutionResult, ExecutionService
from app.utils.time import utcnow


class WorkerService:
    def __init__(self, db: Session, queue_client, worker_id: str, claim_seconds: int = 300, retry_queue=None):
        self.db = db
        self.queue = queue_client
        self.retry_queue = retry_queue if retry_queue is not None else queue_client
        self.worker_id = worker_id
        self.claim_seconds = claim_seconds
        self.tasks = TaskRepository(db)
        self.jobs = JobRepository(db)
        self.executor = ExecutionService()

    def claim_task(self, task_id: str) -> Task | None:
        locked_until = utcnow() + timedelta(seconds=self.claim_seconds)
        started_at = utcnow()

        stmt = (
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == "pending",
            )
            .values(
                status="running",
                locked_by=self.worker_id,
                locked_until=locked_until,
                started_at=started_at,
            )
        )

        result = self.db.execute(stmt)

        if result.rowcount == 0:
            self.db.rollback()
            return None

        self.db.commit()
        self.db.expire_all()

        return self.tasks.get(task_id)

    def process_task_id(self, task_id: str) -> bool:
        task = None

        # Retry because the queue may receive task_id before DB commit is visible.
        for _ in range(5):
            task = self.claim_task(task_id)
            if task:
                break
            time.sleep(0.5)

        if not task:
            return True

        job = None

        for _ in range(5):
            job = self.jobs.get(task.job_id)
            if job:
                break
            time.sleep(0.5)

        if not job:
            self.tasks.mark_failed(task, stdout="", stderr="Job not found", final=True)
            return True

        try:
            result = self.executor.run(job.action_type, job.action_config)
        except Exception as exc:
            result = ExecutionResult(success=False, stdout="", stderr=str(exc))

        if result.success:
            self.tasks.mark_success(task, result.stdout, result.stderr)
            return True

        self._handle_failure(task, job, result.stdout, result.stderr)
        return True

    def _handle_failure(self, task: Task, job, stdout: str | None, stderr: str | None) -> None:
        if task.retry_count < job.max_retries:
            self.tasks.mark_failed(task, stdout, stderr, final=False)

            retry_task = Task(
                job_id=job.id,
                status="pending",
                trigger_type=task.trigger_type,
                retry_count=task.retry_count + 1,
            )

            self.tasks.create_without_commit(retry_task)
            self.db.commit()
            self.db.refresh(retry_task)

            self.retry_queue.send_task(str(retry_task.id))
            return

        self.tasks.mark_failed(task, stdout, stderr, final=True)