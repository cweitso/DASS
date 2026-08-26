from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import selectinload

from app.models.job import Job
from app.models.task import Task
from app.repositories.task_repository import TaskRepository

logger = logging.getLogger(__name__)


def _completed_at(task: Task) -> datetime:
    return task.finished_at or task.created_at


class DependencyScheduler:
    """Fires downstream jobs once all of their upstreams have succeeded."""

    def __init__(self, session_maker, queue_client):
        self.session_maker = session_maker
        self.queue = queue_client

    def trigger_dependent_jobs(self) -> int:
        with self.session_maker() as db:
            tasks = TaskRepository(db).get_unprocessed_successful_tasks()
            if not tasks:
                return 0

            source_jobs = self._load_jobs_with_dependencies(
                db, {str(task.job_id) for task in tasks}
            )

            # Every downstream reachable from the tasks we just observed. Doing this
            # as a set means a diamond (A→C, B→C) evaluates C once per cycle instead
            # of once per completed upstream.
            candidates: dict[str, Job] = {}
            for job in source_jobs.values():
                for downstream in job.downstream_jobs:
                    if downstream.enabled:
                        candidates[str(downstream.id)] = downstream

            for task in tasks:
                TaskRepository(db).mark_processed_for_chaining(task)

            if not candidates:
                return 0

            latest = TaskRepository(db).latest_task_per_job(
                list(
                    {
                        *candidates,
                        *(
                            str(upstream.id)
                            for job in candidates.values()
                            for upstream in job.upstream_jobs
                        ),
                    }
                )
            )

            triggered = 0
            for job in candidates.values():
                if self._dispatch_if_ready(db, job, latest):
                    triggered += 1
            return triggered

    @staticmethod
    def _load_jobs_with_dependencies(db, job_ids: set[str]) -> dict[str, Job]:
        """Load jobs with the two relationship hops the readiness check needs."""
        jobs = (
            db.query(Job)
            .filter(Job.id.in_(job_ids))
            .options(
                selectinload(Job.downstream_jobs).selectinload(Job.upstream_jobs)
            )
            .all()
        )
        return {str(job.id): job for job in jobs}

    def _dispatch_if_ready(
        self, db, job: Job, latest: dict[str, Task]
    ) -> bool:
        upstream_tasks = []
        for upstream in job.upstream_jobs:
            task = latest.get(str(upstream.id))
            if task is None or task.status != "success":
                logger.debug(
                    "Downstream '%s' blocked: upstream '%s' is %s",
                    job.name,
                    upstream.name,
                    task.status if task else "never run",
                )
                return False
            upstream_tasks.append(task)

        if not upstream_tasks:
            return False

        # Idempotency guard. If this job already ran after the newest upstream
        # completion, this cycle is re-observing work that has already been chained.
        ready_at = max(_completed_at(task) for task in upstream_tasks)
        own_latest = latest.get(str(job.id))
        if own_latest is not None and _completed_at(own_latest) >= ready_at:
            return False

        task = Task(
            job_id=str(job.id),
            status="pending",
            trigger_type="dependency",
            retry_count=0,
        )
        TaskRepository(db).create(task)
        self.queue.send_task(str(task.id))
        logger.info(
            "Dependency triggered: job='%s' task=%s (upstreams=%d)",
            job.name,
            task.id,
            len(upstream_tasks),
        )
        return True
