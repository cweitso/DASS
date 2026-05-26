from __future__ import annotations

import json

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


# S4: 給 action_type 對應的 base image。內部 scheduler，固定鏡像即可
_SHELL_IMAGE = "alpine:3"
_HTTP_IMAGE = "curlimages/curl:8.6.0"


def _build_runtime_spec(action_type: str, action_config: dict) -> dict:
    """把 user-facing action_config 翻成 worker 可直接 docker run 的 ContainerSpec dict。

    Worker 端的 ExecutionService 吃 ContainerSpec(**spec_data)，所以這裡產生的 keys
    必須跟 dataclass 對齊：image / command / env / timeout_seconds (+ optional cpu / memory_mb / working_dir)。
    """
    if action_type == "shell":
        return {
            "image": _SHELL_IMAGE,
            "command": ["sh", "-c", action_config["command"]],
            "env": {},
            "timeout_seconds": int(action_config.get("timeout_seconds", 30)),
        }

    if action_type == "http":
        method = str(action_config.get("method", "GET")).upper()
        url = action_config["url"]
        headers = action_config.get("headers") or {}
        body = action_config.get("body")

        cmd: list[str] = ["curl", "-fsS", "-X", method]
        for k, v in headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
        if body is not None:
            body_str = json.dumps(body) if isinstance(body, (dict, list)) else str(body)
            cmd.extend(["-d", body_str])
        cmd.append(url)

        return {
            "image": _HTTP_IMAGE,
            "command": cmd,
            "env": {},
            "timeout_seconds": int(action_config.get("timeout_seconds", 30)),
        }

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"Unsupported action_type: {action_type}",
    )


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
            # S4: 同時填 runtime_spec 給 worker 直接吃
            runtime_spec=_build_runtime_spec(payload.action_type, payload.action_config),
            enabled=payload.enabled,
            concurrency_policy=payload.concurrency_policy,
            max_retries=payload.max_retries,
            next_fire_at=next_cron_time(payload.cron_expression, now),
        )

        return self.jobs.create(job)

    def list_jobs(
        self,
        *,
        page: int,
        page_size: int,
        enabled: bool | None = None,
        action_type: str | None = None,
        concurrency_policy: str | None = None,
        q: str | None = None,
    ) -> tuple[list[Job], int]:
        """列出 Job，支援分頁與篩選。"""
        jobs = self.jobs.list()

        if enabled is not None:
            jobs = [job for job in jobs if job.enabled is enabled]
        if action_type is not None:
            jobs = [job for job in jobs if job.action_type == action_type]
        if concurrency_policy is not None:
            jobs = [job for job in jobs if job.concurrency_policy == concurrency_policy]
        if q:
            needle = q.lower()
            jobs = [job for job in jobs if needle in job.name.lower()]

        total = len(jobs)
        start = (page - 1) * page_size
        end = start + page_size
        return jobs[start:end], total

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

        # S4: action_type / action_config 任何一個動過都要同步 runtime_spec。
        if "action_type" in data or "action_config" in data:
            job.runtime_spec = _build_runtime_spec(action_type, action_config)

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

            assert str(job.id) == str(job_id)
        except (HTTPException, AssertionError):
            raise HTTPException(status_code=404, detail="Job not found")

        return self.tasks.list_by_job(job_id)
