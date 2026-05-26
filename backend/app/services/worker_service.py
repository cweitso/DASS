from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from typing import Callable

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.task import Task
from app.repositories.job_repository import JobRepository
from app.repositories.task_repository import TaskRepository
from app.services.execution_service import ContainerSpec, ExecutionResult, ExecutionService
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


class WorkerService:
    def __init__(self, db: Session, queue_client, worker_id: str, claim_seconds: int = 300, retry_queue=None):
        self.db = db
        # S4 修正：worker 路徑全程「寫後讀」，靠 RoutingSession 的 force_primary 旗標
        # 直接把這個 session 釘在 primary，避開 replica lag 造成 claim/refresh 拿不到剛寫的列。
        self.db.info["force_primary"] = True
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

    def process_task_id(
        self,
        task_id: str,
        extend_visibility: Callable[[int], None] | None = None,
    ) -> bool:
        task = self._claim_task_with_retry(task_id)

        if not task:
            # Claim 失敗，根據 task 當下狀態決定要不要刪 message：
            #   - task 不存在 / 已 terminal → 訊息沒人會處理，刪掉
            #   - task 仍 running 或 pending → 留著 message，讓 SQS visibility 過期後
            #     被下個 worker（在 scheduler 已把 status 改回 pending 的狀態下）重新撈到
            existing = self.tasks.get(task_id)
            if existing is None or existing.status in ("success", "failed", "final_failed"):
                return True
            return False

        job = self._get_job_with_retry(task.job_id)

        if not job:
            self.tasks.mark_failed(task, stdout="", stderr="Job not found", final=True)
            return True

        result = self._execute_job(task, job, extend_visibility)

        if result.success:
            self.tasks.mark_success(task, result.stdout, result.stderr)
            return True

        self._handle_failure(task, job, result.stdout, result.stderr)
        return True

    def _claim_task_with_retry(self, task_id: str) -> Task | None:
        for _ in range(5):
            task = self.claim_task(task_id)
            if task:
                return task
            time.sleep(0.5)
        return None

    def _get_job_with_retry(self, job_id: str):
        for _ in range(5):
            job = self.jobs.get(job_id)
            if job:
                return job
            time.sleep(0.5)
        return None

    def _execute_job(
        self,
        task: Task,
        job,
        extend_visibility: Callable[[int], None] | None = None,
    ) -> ExecutionResult:
        # Worker 完全無腦，只負責把 DB 拿出來的 JSON mapping 到 dataclass 後執行
        spec_data = job.runtime_spec or {}
        try:
            spec = ContainerSpec(**spec_data)
        except Exception as exc:
            return ExecutionResult(success=False, stdout="", stderr=str(exc), exit_code=None)

        # Heartbeat：每 claim_seconds * 0.6 秒同步延長 DB locked_until + SQS visibility，
        # 讓短 visibility_timeout 也撐得住長 job；worker 死掉時 thread 隨之消失，lock/visibility
        # 在一個 visibility window 內自然過期，scheduler.recover_orphans 可以快速 reclaim。
        heartbeat_interval = max(1, int(self.claim_seconds * 0.6))
        stop_event = threading.Event()
        task_id = task.id
        worker_id = self.worker_id
        claim_seconds = self.claim_seconds

        def heartbeat() -> None:
            while not stop_event.wait(heartbeat_interval):
                try:
                    with SessionLocal() as hb_db:
                        hb_db.info["force_primary"] = True
                        hb_db.execute(
                            update(Task)
                            .where(Task.id == task_id, Task.locked_by == worker_id)
                            .values(locked_until=utcnow() + timedelta(seconds=claim_seconds))
                        )
                        hb_db.commit()
                except Exception:
                    logger.exception("Heartbeat DB extend failed task_id=%s", task_id)

                if extend_visibility is not None:
                    try:
                        extend_visibility(claim_seconds)
                    except Exception:
                        logger.exception("Heartbeat SQS extend failed task_id=%s", task_id)

        hb_thread = threading.Thread(target=heartbeat, daemon=True)
        hb_thread.start()
        try:
            return self.executor.run(spec)
        except Exception as exc:
            return ExecutionResult(success=False, stdout="", stderr=str(exc), exit_code=None)
        finally:
            stop_event.set()
            hb_thread.join(timeout=5)

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