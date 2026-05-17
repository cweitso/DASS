from __future__ import annotations

from datetime import datetime
import logging

from sqlalchemy.orm import Session

from app.models.task import Task
from app.repositories.job_repository import JobRepository
from app.repositories.task_repository import TaskRepository
from app.utils.cron import next_cron_time
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


class SchedulerService:
    def __init__(self, db: Session, queue_client, worker_visibility_timeout_seconds: int = 300):
        self.db = db
        self.jobs = JobRepository(db)
        self.tasks = TaskRepository(db)
        self.queue = queue_client
        self.worker_visibility_timeout_seconds = worker_visibility_timeout_seconds

    def recover_orphans(self) -> int:
        """回收所有 locked_until 過期的 running Task：重設為 pending 並重送 Queue。
        #   1. 取得當前時間
        #   2. 用 self.tasks.list_expired_running(now) 找到過期的 task
        #   3. 對每個 task：mark_running_expired_pending → send_task
        #   4. 回傳回收的數量
        # TODO:
        #   改為 concurrent recovery?
        """
        now = utcnow()
        tasks = self.tasks.list_expired_running(now)
        for task in tasks:
            self.tasks.mark_running_expired_pending(task)
            self.queue.send_task(str(task.id))
        return len(tasks)


    def dispatch_due_jobs(self) -> int:
        """掃描所有到期的 enabled Job，為每個建立 Task 並派發到 Queue。
        #   1. 取得當前時間
        #   2. 用 self.jobs.due_jobs(now) 取得到期 Job 列表
        #   3. 對每個 job 呼叫 self._dispatch_job(job, now)
        #   4. 回傳成功派發的數量
        # TODO:
        #   改為 concurrent dispatch ?
        """
        now = utcnow()
        jobs = self.jobs.due_jobs(now)
        counter = 0
        for job in jobs:
            if self._dispatch_job(job, now):
                counter += 1
        return counter

    def _dispatch_job(self, job, now: datetime) -> bool:
        """派發單一 Job：建立 Task、送入 Queue、更新 next_fire_at。
        #   1. 檢查 concurrency_policy：
        #      - 若為 'forbid' 且該 Job 有 running task → 跳過
        #        （仍需更新 next_fire_at）→ 回傳 False
        #   2. 建立 Task(job_id=..., status='pending', trigger_type='scheduled', retry_count=0)
        #   3. self.tasks.create(task)
        #   4. 更新 job.next_fire_at = next_cron_time(...)
        #   5. self.jobs.update(job)
        #   6. self.queue.send_task(str(task.id))
        #   7. 回傳 True
        """
        job.next_fire_at = next_cron_time(job.cron_expression, now)
        self.jobs.update(job)
        if job.concurrency_policy == "forbid" and self.tasks.count_running_for_job(job.id) > 0:
            return False
        task = Task(job_id=str(job.id), status="pending", trigger_type="scheduled", retry_count=0)
        self.tasks.create(task)
        self.queue.send_task(str(task.id))
        return True
        
