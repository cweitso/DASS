from __future__ import annotations

from croniter import croniter
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.job import Job
from app.models.task import Task
from app.repositories.job_repository import JobRepository
from app.repositories.task_repository import TaskRepository
from app.schemas.job import HttpActionConfig, JobCreate, JobUpdate, ShellActionConfig
from app.utils.cron import next_cron_time
from app.utils.time import utcnow


class JobService:
    def __init__(self, db: Session):
        self.db = db
        self.jobs = JobRepository(db)
        self.tasks = TaskRepository(db)

    def create_job(self, payload: JobCreate) -> Job:
        """根據 JobCreate schema 建立新 Job。

        1. 驗證 cron_expression 是否合法（croniter.is_valid），不合法回 422
        2. 用 payload 欄位建立 Job model instance
           - next_fire_at 用 next_cron_time(cron_expression, utcnow()) 計算
        3. 透過 self.jobs.create() 寫入 DB
        4. 回傳建立好的 Job
        """

        # if payload.concurrency_policy == "replace":
        #     # TODO: implement replace semantics explicitly; for now it is accepted but behaves like allow.
        #     pass

        if not croniter.is_valid(payload.cron_expression):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid cron expression",
            )

        now = utcnow()
        job = Job(
            name=payload.name,
            cron_expression=payload.cron_expression,
            action_type=payload.action_type,
            action_config=payload.action_config,
            enabled=payload.enabled,
            concurrency_policy=payload.concurrency_policy,
            max_retries=payload.max_retries,
            next_fire_at=next_cron_time(payload.cron_expression, now),
        )

        return self.jobs.create(job)

    def list_jobs(self) -> list[Job]:
        """列出所有 Job。

        直接呼叫 self.jobs.list()
        """

        return self.jobs.list()

    def get_job(self, job_id: str) -> Job:
        """取得單一 Job，不存在則 raise 404。

        呼叫 self.jobs.get(job_id)
        若 None → raise HTTPException(status_code=404, detail="Job not found")
        """

        job = self.jobs.get(job_id)

        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        return job

    def update_job(self, job_id: str, payload: JobUpdate) -> Job:
        """更新 Job 欄位。需驗證 cron_expression 和 action_config 的合法性。

        1. 先 get_job 確認存在
        2. 用 payload.model_dump(exclude_unset=True) 取得要更新的欄位
        3. 若有 cron_expression，驗證合法性
        4. 若有 action_type/action_config，用對應 schema 驗證
        5. setattr 更新 job 欄位
        6. 若 cron 改了，重算 next_fire_at
        7. self.jobs.update(job)
        """

        job = self.get_job(job_id)
        data = payload.model_dump(exclude_unset=True)

        if "cron_expression" in data and not croniter.is_valid(data["cron_expression"]):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid cron expression",
            )

        action_type = data.get("action_type", job.action_type)
        action_config = data.get("action_config", job.action_config)

        if action_type == "http":
            HttpActionConfig.model_validate(action_config)
        elif action_type == "shell":
            ShellActionConfig.model_validate(action_config)
        for key, value in data.items():
            setattr(job, key, value)
        if payload.cron_expression:
            job.next_fire_at = next_cron_time(payload.cron_expression, utcnow())

        return self.jobs.update(job)

    def delete_job(self, job_id: str) -> None:
        """刪除指定 Job。

        get_job → self.jobs.delete()
        """

        job = self.get_job(job_id)
        self.jobs.delete(job)

    def trigger_job(self, job_id: str, queue_client) -> Task:
        """手動觸發 Job，建立一筆 pending Task 並送入 Queue。

        1. get_job 確認存在
        2. 建立 Task(job_id=..., status='pending', trigger_type='manual', retry_count=0)
        3. self.tasks.create(task)
        4. queue_client.send_task(str(task.id))
        5. 回傳 task
        """

        job = self.get_job(job_id)

        task = Task(
            job_id=job.id,
            status="pending",
            trigger_type="manual",
            retry_count=0,
        )

        task = self.tasks.create(task)
        queue_client.send_task(str(task.id))

        return task

    def list_job_tasks(self, job_id: str) -> list[Task]:
        """列出指定 Job 的所有 Task 歷史。

        先 get_job 確認存在，再 self.tasks.list_by_job(job_id)
        """

        try:
            job = self.get_job(job_id)

            assert job.id == job_id
        except (HTTPException, AssertionError):
            raise HTTPException(status_code=404, detail="Job not found")

        return self.tasks.list_by_job(job_id)
