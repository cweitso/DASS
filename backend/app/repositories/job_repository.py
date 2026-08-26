from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import force_primary
from app.models.job import Job, job_dependencies


class JobRepository:
    def __init__(self, db: Session):
        self.db = db

    # ── Writes ────────────────────────────────────────────────────────────────
    # create/update refresh the instance right after committing, which is a read of
    # a row this session just wrote. force_primary keeps that read off the replica,
    # where replication lag would either return stale values or fail the refresh.

    def create(self, job: Job) -> Job:
        with force_primary(self.db):
            self.db.add(job)
            self.db.commit()
            self.db.refresh(job)
        return job

    def update(self, job: Job) -> Job:
        with force_primary(self.db):
            self.db.add(job)
            self.db.commit()
            self.db.refresh(job)
        return job

    def delete(self, job: Job) -> None:
        self.db.delete(job)
        self.db.commit()

    # ── Reads ─────────────────────────────────────────────────────────────────

    def get(self, job_id: str) -> Job | None:
        return self.db.get(Job, job_id)

    def list_paginated(
        self,
        *,
        page: int,
        page_size: int,
        enabled: bool | None = None,
        action_type: str | None = None,
        concurrency_policy: str | None = None,
        q: str | None = None,
    ) -> tuple[list[Job], int]:
        """One page of jobs plus the filtered total.

        Filtering, counting and paging all happen in SQL. Loading the whole table
        and slicing in Python does not survive a few thousand jobs.
        """
        conditions = []
        if enabled is not None:
            conditions.append(Job.enabled == enabled)
        if action_type is not None:
            conditions.append(Job.action_type == action_type)
        if concurrency_policy is not None:
            conditions.append(Job.concurrency_policy == concurrency_policy)
        if q:
            # lower() + like works on both PostgreSQL and SQLite, unlike ILIKE.
            conditions.append(func.lower(Job.name).like(f"%{q.lower()}%"))

        total = (
            self.db.scalar(select(func.count()).select_from(Job).where(*conditions)) or 0
        )
        stmt = (
            select(Job)
            .where(*conditions)
            .order_by(Job.created_at.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        return list(self.db.scalars(stmt).all()), total

    def list_updated_since(self, since: datetime | None) -> list[Job]:
        """Scheduled jobs the scheduler needs to (re)load into its heap.

        Only job_type='scheduled': one-time jobs are dispatched by the API, and
        re-dispatching them here would double-run them. Disabled jobs are included
        on purpose so sync_jobs can evict them from the heap.
        """
        stmt = select(Job).where(Job.job_type == "scheduled")
        if since is not None:
            stmt = stmt.where(Job.updated_at >= since)
        return list(self.db.scalars(stmt).all())

    def get_all_descendants(self, job_id: str) -> set[str]:
        """Every job reachable downstream of this one, at any depth.

        A recursive CTE with UNION (not UNION ALL) so an existing cycle terminates
        instead of recursing forever.
        """
        first_hop = (
            select(job_dependencies.c.downstream_job_id)
            .where(job_dependencies.c.upstream_job_id == job_id)
            .cte(name="descendants", recursive=True)
        )
        deeper = select(job_dependencies.c.downstream_job_id).join(
            first_hop,
            job_dependencies.c.upstream_job_id == first_hop.c.downstream_job_id,
        )
        rows = self.db.execute(select(first_hop.union(deeper))).all()
        return {str(row[0]) for row in rows}
