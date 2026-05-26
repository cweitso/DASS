from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import GUID, JSONBCompat


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("name", name="uq_jobs_name"),)

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled", index=True) # 任務類型，預設為定時任務，並建立索引 (B+ 樹)
    cron_expression: Mapped[str | None] = mapped_column(String(255), nullable=True) # 即時任務不需要 Cron，所以改為允許為空 (nullable=True)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    action_config: Mapped[dict] = mapped_column(JSONBCompat(), nullable=False)
    # S4: ContainerSpec 預先翻好的字典；JobService 在 create/update 時填入，worker 直接吃。
    runtime_spec: Mapped[dict | None] = mapped_column(JSONBCompat(), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    concurrency_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True) # 即時任務不需要下次執行時間，改為允許為空，並建立時間排序索引 (B+ 樹)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    tasks = relationship("Task", back_populates="job", cascade="all, delete-orphan")

