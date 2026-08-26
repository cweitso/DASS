from __future__ import annotations

import heapq
import logging
from datetime import UTC, datetime

from app.models.task import Task
from app.repositories.job_repository import JobRepository
from app.repositories.task_repository import TaskRepository
from app.utils.cron import next_cron_time
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

# Heap entries and cached next_fire_at are compared as float timestamps, so allow a
# little slack when deciding whether they still describe the same firing.
_TIMESTAMP_EPSILON = 0.001


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat them as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class CronScheduler:
    """Dispatches cron jobs from an in-memory heap of next firing times.

    The heap is a cache over the jobs table, refreshed incrementally by sync_jobs.
    Stale heap entries are not removed — they are recognised and skipped on pop
    (tombstoning), which keeps sync cheap.
    """

    def __init__(
        self, session_maker, queue_client, worker_visibility_timeout_seconds: int = 300
    ):
        self.session_maker = session_maker
        self.queue = queue_client
        self.worker_visibility_timeout_seconds = worker_visibility_timeout_seconds
        self._heap: list[tuple[float, str]] = []
        self._job_cache: dict[str, object] = {}
        self.last_sync_at: datetime | None = None

    def sync_jobs(self) -> None:
        """Pull jobs changed since the last sync into the cache and heap."""
        with self.session_maker() as db:
            jobs = JobRepository(db).list_updated_since(self.last_sync_at)
            db.expunge_all()

        for job in jobs:
            job_id = str(job.id)

            if not job.enabled:
                # Leaving the heap entry behind is fine: the pop path drops entries
                # whose job is no longer cached.
                self._job_cache.pop(job_id, None)
                continue

            job.next_fire_at = _as_utc(job.next_fire_at)
            if job.next_fire_at is not None and self._is_new_firing(job_id, job.next_fire_at):
                heapq.heappush(self._heap, (job.next_fire_at.timestamp(), job_id))
            self._job_cache[job_id] = job

        self.last_sync_at = utcnow()

    def _is_new_firing(self, job_id: str, next_fire_at: datetime) -> bool:
        cached = self._job_cache.get(job_id)
        if cached is None or cached.next_fire_at is None:
            return True
        return (
            abs(cached.next_fire_at.timestamp() - next_fire_at.timestamp())
            > _TIMESTAMP_EPSILON
        )

    def recover_orphans(self) -> int:
        """Return tasks whose lock expired to pending so they can be claimed again.

        No message is re-sent. The worker heartbeat extends the DB lock and the queue
        visibility together, so they expire together and the queue re-delivers on its
        own. Re-sending here would risk running the task twice.
        """
        with self.session_maker() as db:
            repo = TaskRepository(db)
            tasks = repo.list_expired_running(utcnow())
            for task in tasks:
                repo.mark_running_expired_pending(task)
            return len(tasks)

    def dispatch_due_jobs(self) -> int:
        """Fire every job whose next_fire_at has passed, then reschedule it."""
        now = utcnow()
        dispatched = 0

        while self._heap:
            scheduled_ts, job_id = self._heap[0]
            if scheduled_ts > now.timestamp():
                break
            heapq.heappop(self._heap)

            job = self._job_cache.get(job_id)
            if job is None:
                logger.debug("Skipping job %s: deleted or disabled", job_id)
                continue
            if abs(job.next_fire_at.timestamp() - scheduled_ts) > _TIMESTAMP_EPSILON:
                logger.debug("Skipping stale heap entry for job %s", job_id)
                continue

            with self.session_maker() as db:
                job = db.merge(job)
                fired = self._dispatch(db, job, now)

                # Reschedule whether or not the run happened: _dispatch has already
                # advanced next_fire_at, and a skipped run must not stall the job.
                job.next_fire_at = _as_utc(job.next_fire_at)
                self._job_cache[job_id] = job
                if job.next_fire_at is not None:
                    heapq.heappush(self._heap, (job.next_fire_at.timestamp(), job_id))

            dispatched += fired

        if dispatched:
            logger.info("Dispatched %d job(s)", dispatched)
        return dispatched

    def _dispatch(self, db, job, now: datetime) -> int:
        """Advance the job's schedule and enqueue a task unless concurrency forbids it."""
        jobs = JobRepository(db)
        tasks = TaskRepository(db)

        job.next_fire_at = next_cron_time(job.cron_expression, now)
        jobs.update(job)

        if job.concurrency_policy == "forbid" and tasks.count_running_for_job(job.id):
            logger.info("Skipping job '%s': previous run still in flight", job.name)
            return 0

        task = tasks.create(
            Task(
                job_id=str(job.id),
                status="pending",
                trigger_type="scheduled",
                retry_count=0,
            )
        )
        self.queue.send_task(str(task.id))
        return 1
