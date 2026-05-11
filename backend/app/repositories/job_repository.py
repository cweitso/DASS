from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.job import Job


class JobRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, job: Job) -> Job:
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

    def get(self, job_id: str) -> Job | None:
        # 透過主鍵 (Primary Key) 找單一資料，SQLAlchemy 提供了一個專屬的超級捷徑：self.db.get()
        return self.db.get(Job, job_id)
        

    def delete(self, job: Job) -> None:
        
        self.db.delete(job)
        
        self.db.commit()
        

    def update(self, job: Job) -> Job:
        # SQLAlchemy 會檢查傳進來的這個 job 物件，如果這個 job 沒有 ID，執行 INSERT，
        # 反之會自動對比哪裡被改過，然後發送 UPDATE 指令只修改那些欄位。
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def due_jobs(self, now: datetime) -> list[Job]:
        
        # 1. 建立查詢語句：選擇 Job -> 加入過濾條件 -> 加入排序
        stmt = select(Job).where(Job.enabled == True, Job.next_fire_at <= now ).order_by(Job.next_fire_at.asc())
        
        result = self.db.execute(stmt)
        return result.scalars().all()
