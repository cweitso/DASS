from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Callable

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.session import heartbeat_engine
from app.models.task import Task
from app.repositories.job_repository import JobRepository
from app.repositories.task_repository import TaskRepository
from app.services.execution_factory import get_execution_service
from app.services.execution_service import ContainerSpec, ExecutionResult
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

# Claim and job lookups race against the writer that just enqueued the message,
# so retry briefly before giving up.
_LOOKUP_ATTEMPTS = 5
_LOOKUP_BACKOFF_SECONDS = 0.5

# Captured stdout/stderr is stored verbatim; cap it so one chatty job cannot write
# an unbounded blob into every task row.
_MAX_OUTPUT_CHARS = 64_000
_TRUNCATION_NOTICE = "\n...[truncated]"


def _truncate(text: str | None) -> str | None:
    if text is None or len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + _TRUNCATION_NOTICE


class WorkerService:
    """Claims one task, runs it as a container, and records the outcome."""

    def __init__(
        self,
        db: Session,
        queue_client,
        worker_id: str,
        claim_seconds: int = 300,
        retry_queue=None,
    ):
        self.db = db
        # The worker path is read-after-write from end to end (claim, then re-read the
        # row it just claimed). Pin the session to the primary so replication lag can
        # never hide a row this session wrote.
        self.db.info["force_primary"] = True
        self.queue = queue_client
        self.retry_queue = retry_queue if retry_queue is not None else queue_client
        self.worker_id = worker_id
        self.claim_seconds = claim_seconds
        self.tasks = TaskRepository(db)
        self.jobs = JobRepository(db)
        # The factory picks docker or kubernetes from DASS_EXECUTION_BACKEND and
        # injects DASS_DOCKER_NETWORK.
        self.executor = get_execution_service()

    # ── Claiming ──────────────────────────────────────────────────────────────

    def claim_task(self, task_id: str) -> Task | None:
        """Atomically move a task from pending to running. Returns None if lost."""
        result = self.db.execute(
            update(Task)
            .where(Task.id == task_id, Task.status == "pending")
            .values(
                status="running",
                locked_by=self.worker_id,
                locked_until=utcnow() + timedelta(seconds=self.claim_seconds),
                started_at=utcnow(),
            )
        )

        if result.rowcount == 0:
            self.db.rollback()
            return None

        self.db.commit()
        self.db.expire_all()
        return self.tasks.get(task_id)

    async def _claim_with_retry(self, task_id: str) -> Task | None:
        """Claim, retrying only while the row could still become claimable.

        A message can arrive before its row is visible, which is what the retries
        are for. A row that exists but is not pending belongs to someone else or is
        already finished, so retrying would just hold a pool slot for nothing.
        """
        for attempt in range(_LOOKUP_ATTEMPTS):
            task = self.claim_task(task_id)
            if task is not None:
                return task
            existing = self.tasks.get(task_id)
            if existing is not None and existing.status != "pending":
                return None
            if attempt < _LOOKUP_ATTEMPTS - 1:
                await self._backoff()
        return None

    async def _get_job_with_retry(self, job_id: str):
        for attempt in range(_LOOKUP_ATTEMPTS):
            job = self.jobs.get(job_id)
            if job is not None:
                return job
            if attempt < _LOOKUP_ATTEMPTS - 1:
                await self._backoff()
        return None

    @staticmethod
    async def _backoff() -> None:
        # asyncio.sleep, not time.sleep: this coroutine shares its event loop with
        # the other tasks running in the same queue pool.
        await asyncio.sleep(_LOOKUP_BACKOFF_SECONDS)

    # ── Processing ────────────────────────────────────────────────────────────

    async def process_task_id_async(
        self,
        task_id: str,
        extend_visibility: Callable[[int], None] | None = None,
    ) -> bool:
        """Run one task. Returns True when the queue message can be deleted."""
        task = await self._claim_with_retry(task_id)

        if not task:
            # Claim lost. Whether the message is still useful depends on the task:
            # gone or terminal means nobody will process it, so drop the message;
            # still pending or running means leave it for the next visibility window.
            existing = self.tasks.get(task_id)
            return existing is None or existing.status in (
                "success",
                "failed",
                "final_failed",
            )

        job = await self._get_job_with_retry(task.job_id)
        if not job:
            self.tasks.mark_failed(task, stdout="", stderr="Job not found", final=True)
            return True

        result = await self._execute(task, job, extend_visibility)

        if result.success:
            self.tasks.mark_success(
                task, _truncate(result.stdout), _truncate(result.stderr)
            )
        else:
            self._handle_failure(
                task, job, _truncate(result.stdout), _truncate(result.stderr)
            )
        return True

    def process_task_id(
        self,
        task_id: str,
        extend_visibility: Callable[[int], None] | None = None,
    ) -> bool:
        """Blocking wrapper around process_task_id_async, for sync callers and tests."""
        return asyncio.run(self.process_task_id_async(task_id, extend_visibility))

    # ── Execution ─────────────────────────────────────────────────────────────

    async def _execute(
        self,
        task: Task,
        job,
        extend_visibility: Callable[[int], None] | None,
    ) -> ExecutionResult:
        """Run the job's container spec, keeping the lock alive while it runs."""
        try:
            spec = ContainerSpec(**(job.runtime_spec or {}))
        except Exception as exc:  # noqa: BLE001 — a malformed spec is a task failure
            return ExecutionResult(success=False, stdout="", stderr=str(exc))

        heartbeat = asyncio.create_task(self._heartbeat(task.id, extend_visibility))
        try:
            if hasattr(self.executor, "run_async"):
                return await self.executor.run_async(spec)
            # Executors without async support run in a thread so the event loop
            # stays responsive for the other tasks in this pool.
            return await asyncio.to_thread(self.executor.run, spec)
        except Exception as exc:  # noqa: BLE001 — surface as a failed task, not a crash
            return ExecutionResult(success=False, stdout="", stderr=str(exc))
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def _heartbeat(
        self,
        task_id: str,
        extend_visibility: Callable[[int], None] | None,
    ) -> None:
        """Extend the DB lock and the queue message visibility while a task runs.

        Both are extended together so a crashed worker loses them together: the
        message becomes visible again and the scheduler can reclaim the row.
        """
        interval = max(1, int(self.claim_seconds * 0.6))
        while True:
            await asyncio.sleep(interval)

            try:
                # heartbeat_engine is a small dedicated pool — extending a lock must
                # never queue behind the connections the running tasks are holding.
                with heartbeat_engine.begin() as conn:
                    conn.execute(
                        update(Task)
                        .where(Task.id == task_id, Task.locked_by == self.worker_id)
                        .values(
                            locked_until=utcnow()
                            + timedelta(seconds=self.claim_seconds)
                        )
                    )
            except Exception:
                logger.exception("Heartbeat DB extend failed task_id=%s", task_id)

            if extend_visibility is not None:
                try:
                    extend_visibility(self.claim_seconds)
                except Exception:
                    logger.exception(
                        "Heartbeat queue extend failed task_id=%s", task_id
                    )

    # ── Failure handling ──────────────────────────────────────────────────────

    def _handle_failure(
        self, task: Task, job, stdout: str | None, stderr: str | None
    ) -> None:
        if task.retry_count >= job.max_retries:
            self.tasks.mark_failed(task, stdout, stderr, final=True)
            return

        self.tasks.mark_failed(task, stdout, stderr, final=False)
        retry_task = Task(
            job_id=job.id,
            status="pending",
            trigger_type=task.trigger_type,
            retry_count=task.retry_count + 1,
        )
        self.tasks.create(retry_task)
        self.retry_queue.send_task(str(retry_task.id))
