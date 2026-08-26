from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    func,
    ForeignKey,
    Table,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import GUID, JSONBCompat


job_dependencies = Table(
    "job_dependencies",
    Base.metadata,
    Column(
        "upstream_job_id",
        GUID(),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "downstream_job_id",
        GUID(),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("name", name="uq_jobs_name"),)

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # "scheduled" (cron-driven) or "normal" (one-time). Indexed because the
    # scheduler filters on it every sync.
    job_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="scheduled", index=True
    )
    # Null for one-time jobs, which have no schedule.
    cron_expression: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    action_config: Mapped[dict] = mapped_column(JSONBCompat(), nullable=False)
    # Pre-translated ContainerSpec. JobService fills it on create/update so the
    # worker can execute a job without re-interpreting action_config.
    runtime_spec: Mapped[dict | None] = mapped_column(JSONBCompat(), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    concurrency_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Null for one-time jobs. Indexed for the scheduler's ordering by due time.
    next_fire_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    tasks = relationship("Task", back_populates="job", cascade="all, delete-orphan")

    # Jobs this one may trigger once it succeeds.
    downstream_jobs = relationship(
        "Job",
        secondary=job_dependencies,
        primaryjoin="Job.id == job_dependencies.c.upstream_job_id",
        secondaryjoin="Job.id == job_dependencies.c.downstream_job_id",
        back_populates="upstream_jobs",
    )
    # Jobs that must all succeed before this one runs.
    upstream_jobs = relationship(
        "Job",
        secondary=job_dependencies,
        primaryjoin="Job.id == job_dependencies.c.downstream_job_id",
        secondaryjoin="Job.id == job_dependencies.c.upstream_job_id",
        back_populates="downstream_jobs",
    )

    @property
    def upstream_job_ids(self) -> list[str]:
        return [str(job.id) for job in self.upstream_jobs]

    @property
    def downstream_job_ids(self) -> list[str]:
        return [str(job.id) for job in self.downstream_jobs]
