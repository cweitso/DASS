from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.task import Task
from app.models.job import Job


class TaskRepository:
    def __init__(self, db: Session):
        self.db = db

    # ── 基本 CRUD ──────────────────────────────────────

    def create(self, task: Task) -> Task:
        
        self.db.add(task)
        self.db.commit()
        self.db.refresh(task)
        return task

    def create_without_commit(self, task: Task) -> Task:
        
        self.db.add(task)
        return task

    def get(self, task_id: str) -> Task | None:
        
        return self.db.get(Task, task_id)

    # ── 查詢 ───────────────────────────────────────────

    def list_by_job(self, job_id: str) -> list[Task]:
    
        stmt = select(Task).where(Task.job_id == job_id).order_by(Task.created_at.desc())
        return list(self.db.scalars(stmt).all())

    def list_recent(self, limit: int = 20) -> list[Task]:
    
        stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)
        return list(self.db.scalars(stmt).all())

    def count_running_for_job(self, job_id: str) -> int:
        
        stmt = select(func.count()).select_from(Task).where(Task.job_id == job_id, Task.status == 'running')
        return self.db.scalar(stmt)

    # ── 原子性 Claim（最關鍵！）─────────────────────────

    def claim_pending(self, task_id: str, worker_id: str, locked_until: datetime) -> Task | None:
        
        stmt = (
            update(Task)
            .where(Task.id == task_id, Task.status == 'pending')
            .values(
                status = 'running',
                locked_by = worker_id,
                locked_until = locked_until,
                started_at = func.now()
            )
            # 這行咒語強迫 SQLAlchemy 放棄快取，直接重新同步, 用 evaluate 可以提升效能( 少一次 select )但會導致快取未同步
            .execution_options(synchronize_session="fetch")
        )
        
        result = self.db.execute(stmt)
        
        if result.rowcount == 0:
            self.db.commit()
            return None
        else:
            self.db.commit()
            return self.get(task_id)

    # ── 狀態更新 ────────────────────────────────────────

    def mark_success(self, task: Task, stdout: str | None, stderr: str | None) -> Task:
        
        task.status = 'success'
        task.stdout = stdout
        task.stderr = stderr
        task.locked_by = None
        task.locked_until = None
        task.finished_at = datetime.now(UTC)
        self.db.commit()
        self.db.refresh(task)
        return task

    def mark_failed(self, task: Task, stdout: str | None, stderr: str | None, final: bool = False) -> Task:
        
        task.status = 'final_failed' if final else 'failed'
        task.stdout = stdout
        task.stderr = stderr
        task.locked_by = None
        task.locked_until = None
        task.finished_at = datetime.now(UTC)
        self.db.commit()
        self.db.refresh(task)
        return task
        

    def mark_running_expired_pending(self, task: Task) -> Task:
        
        task.status = 'pending'
        task.locked_by = None
        task.locked_until = None
        task.started_at = None
        self.db.commit()
        self.db.refresh(task)
        return task

    def list_expired_running(self, now: datetime) -> list[Task]:
        
        stmt = select(Task).where(Task.status == 'running', Task.locked_until.is_not(None), Task.locked_until < now)
        return list(self.db.scalars(stmt).all())
    
    