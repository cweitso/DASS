from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
import json

from sqlalchemy.orm import Session

from app.models.task import Task
from app.repositories.job_repository import JobRepository
from app.repositories.task_repository import TaskRepository
from app.services.execution_service import ExecutionResult, ExecutionService
from app.utils.time import utcnow


class WorkerService:
    def __init__(self, db: Session, queue_client, worker_id: str, claim_seconds: int = 300):
        self.db = db
        self.queue = queue_client
        self.worker_id = worker_id
        self.claim_seconds = claim_seconds
        self.tasks = TaskRepository(db)
        self.jobs = JobRepository(db)
        self.executor = ExecutionService()

    # use lock to make sure wont race condition, only one worker can claim the task
    def claim_task(self, task_id: str) -> Task | None:
        locked_until = utcnow() + timedelta(seconds=self.claim_seconds)
        return self.tasks.claim_pending(task_id, self.worker_id, locked_until)

    def process_task_id(self, task_id: str) -> bool:
        # 重試幾次，因為有可能 Message Queue 已經推播了 task_id，但是 DB 還沒 Commit 完成
        task = None
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
            self.queue.send_task(str(retry_task.id))
            return
        self.tasks.mark_failed(task, stdout, stderr, final=True)
