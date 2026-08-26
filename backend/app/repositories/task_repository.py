from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.models.task import Task


class TaskRepository:
    def __init__(self, db: Session):
        self.db = db

    # ── Writes ────────────────────────────────────────────────────────────────

    def create(self, task: Task) -> Task:
        # No refresh: the caller only needs `id` (client-side uuid4) and `status`
        # (set on the instance), and with expire_on_commit=False both survive the
        # commit. Refreshing here used to route a SELECT to the replica and fail on
        # replication lag under load.
        self.db.add(task)
        self.db.commit()
        return task

    def mark_success(self, task: Task, stdout: str | None, stderr: str | None) -> Task:
        return self._finish(task, "success", stdout, stderr)

    def mark_failed(
        self, task: Task, stdout: str | None, stderr: str | None, final: bool = False
    ) -> Task:
        return self._finish(task, "final_failed" if final else "failed", stdout, stderr)

    def _finish(
        self, task: Task, status: str, stdout: str | None, stderr: str | None
    ) -> Task:
        task.status = status
        task.stdout = stdout
        task.stderr = stderr
        task.locked_by = None
        task.locked_until = None
        task.finished_at = datetime.now(UTC)
        self.db.commit()
        self.db.refresh(task)
        return task

    def mark_running_expired_pending(self, task: Task) -> Task:
        """Release an expired lock so the task can be claimed again."""
        task.status = "pending"
        task.locked_by = None
        task.locked_until = None
        task.started_at = None
        self.db.commit()
        self.db.refresh(task)
        return task

    def mark_processed_for_chaining(self, task: Task) -> Task:
        task.processed_for_chaining = True
        self.db.commit()
        return task

    # ── Reads ─────────────────────────────────────────────────────────────────

    def get(self, task_id: str) -> Task | None:
        return self.db.get(Task, task_id)

    def list_by_job(self, job_id: str) -> list[Task]:
        stmt = (
            select(Task).where(Task.job_id == job_id).order_by(Task.created_at.desc())
        )
        return list(self.db.scalars(stmt).all())

    def count_running_for_job(self, job_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(Task)
            .where(Task.job_id == job_id, Task.status == "running")
        )
        return self.db.scalar(stmt) or 0

    def list_expired_running(self, now: datetime) -> list[Task]:
        stmt = select(Task).where(
            Task.status == "running",
            Task.locked_until.is_not(None),
            Task.locked_until < now,
        )
        return list(self.db.scalars(stmt).all())

    def get_unprocessed_successful_tasks(self, limit: int = 100) -> list[Task]:
        """Successful tasks whose downstream dependencies have not been evaluated."""
        stmt = (
            select(Task)
            .where(Task.status == "success", Task.processed_for_chaining.is_(False))
            .order_by(Task.created_at)
            .limit(limit)
        )
        return list(self.db.scalars(stmt).all())

    def latest_task_per_job(self, job_ids: list[str]) -> dict[str, Task]:
        """Most recent task for each of the given jobs, in a single query.

        The dependency scheduler needs this for every upstream and downstream of
        every job it evaluates; querying per job turned one scheduler cycle into
        hundreds of round trips.
        """
        if not job_ids:
            return {}

        ranked = (
            select(
                Task,
                func.row_number()
                .over(partition_by=Task.job_id, order_by=Task.created_at.desc())
                .label("rank"),
            )
            .where(Task.job_id.in_(job_ids))
            .subquery()
        )
        ranked_task = aliased(Task, ranked)
        stmt = select(ranked_task).where(ranked.c.rank == 1)
        return {str(task.job_id): task for task in self.db.scalars(stmt).all()}
