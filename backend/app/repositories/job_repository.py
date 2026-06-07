from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.job import Job


@contextmanager
def _force_primary(db: Session) -> Iterator[None]:
    previous = db.info.get("force_primary")
    had_previous = "force_primary" in db.info
    db.info["force_primary"] = True
    try:
        yield
    finally:
        if had_previous:
            db.info["force_primary"] = previous
        else:
            db.info.pop("force_primary", None)


class JobRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, job: Job) -> Job:
        with _force_primary(self.db):
            # 1. 用 self.db.add() 將傳進來的 job 加入 session
            self.db.add(job)

            # 2. 執行 commit 提交變更到資料庫
            self.db.commit()

            # 3. 執行 refresh 取得資料庫生成的欄位（例如 id）
            self.db.refresh(job)

        # 4. 回傳處理完的 job 物件
        return job

    def list(self) -> list[Job]:
        # 1. 建立查詢語句並加上排序條件
        stmt = select(Job).order_by(Job.created_at.desc())

        # 2. 透過 self.db 執行語句，並將結果轉換為 Job 物件列表
        result = self.db.execute(stmt)

        # 3. 抽取出物件並轉為清單回傳
        return result.scalars().all()

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
        """SQL 端分頁 + 過濾，回傳 (該頁 jobs, 過濾後總數)。

        取代「撈整張 jobs 表進記憶體再 Python 切片」的舊作法——資料量一大（例如 10k+ jobs）
        每次列表請求都載入全表會很慢且耗記憶體。過濾、計數、分頁全部下推到 SQL。
        """
        conditions = []
        if enabled is not None:
            conditions.append(Job.enabled == enabled)
        if action_type is not None:
            conditions.append(Job.action_type == action_type)
        if concurrency_policy is not None:
            conditions.append(Job.concurrency_policy == concurrency_policy)
        if q:
            # func.lower(...).like 在 SQLite / Postgres 都可用（避開 Postgres 限定的 ILIKE）
            conditions.append(func.lower(Job.name).like(f"%{q.lower()}%"))

        count_stmt = select(func.count()).select_from(Job)
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        total = self.db.scalar(count_stmt) or 0

        stmt = select(Job)
        if conditions:
            stmt = stmt.where(*conditions)
        stmt = stmt.order_by(Job.created_at.desc()).limit(page_size).offset((page - 1) * page_size)
        jobs = list(self.db.scalars(stmt).all())
        return jobs, total

    def get(self, job_id: str) -> Job | None:
        # 透過主鍵 (Primary Key) 找單一資料，SQLAlchemy 提供了一個專屬的超級捷徑：self.db.get()
        return self.db.get(Job, job_id)

    def delete(self, job: Job) -> None:
        self.db.delete(job)
        self.db.commit()

    def update(self, job: Job) -> Job:
        # SQLAlchemy 會檢查傳進來的這個 job 物件，如果這個 job 沒有 ID，執行 INSERT，
        # 反之會自動對比哪裡被改過，然後發送 UPDATE 指令只修改那些欄位。
        with _force_primary(self.db):
            self.db.add(job)
            self.db.commit()
            self.db.refresh(job)
        return job

    def list_updated_since(self, since: datetime | None) -> list[Job]:
        # 只同步定時任務 (job_type='scheduled') 給 scheduler heap；normal/manual job
        # 由 API 手動觸發直接進 normal queue，不該被排程器撈進來重複派發。
        # 不預設過濾 enabled：被停用的定時任務仍要回傳，sync_jobs 才能把它從 cache/heap
        # 清掉 (Soft Delete / Tombstone)。
        stmt = select(Job).where(Job.job_type == "scheduled")

        # 若有提供 since，則只抓取該時間點之後異動過的任務 (增量拉取)
        if since is not None:
            stmt = stmt.where(Job.updated_at >= since)

        result = self.db.execute(stmt)
        return result.scalars().all()
