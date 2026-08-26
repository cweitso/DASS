from __future__ import annotations

import json
from datetime import datetime

from croniter import croniter
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.job import Job
from app.models.task import Task
from app.queue.factory import get_normal_queue_client
from app.repositories.job_repository import JobRepository
from app.repositories.task_repository import TaskRepository
from app.schemas.job import HttpActionConfig, JobCreate, JobUpdate, ShellActionConfig
from app.utils.cron import next_cron_time
from app.utils.time import utcnow

# Fixed base images per action type. This is an internal scheduler, so the images
# are pinned here rather than exposed to job authors.
_SHELL_IMAGE = "alpine:3"
_HTTP_IMAGE = "curlimages/curl:8.6.0"

_ACTION_CONFIG_SCHEMAS = {"http": HttpActionConfig, "shell": ShellActionConfig}


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
    )


def _build_runtime_spec(action_type: str, action_config: dict) -> dict:
    """Translate a user-facing action into the ContainerSpec the worker executes.

    Keys must match the ContainerSpec dataclass: the worker does
    `ContainerSpec(**job.runtime_spec)` and nothing else.
    """
    if action_type == "shell":
        if not get_settings().shell_execution_enabled:
            raise _unprocessable(
                "Shell actions are disabled. Set DASS_SHELL_EXECUTION_ENABLED=true to allow them."
            )
        return {
            "image": _SHELL_IMAGE,
            "command": ["sh", "-c", action_config["command"]],
            "env": {},
            "timeout_seconds": int(action_config.get("timeout_seconds", 30)),
        }

    if action_type == "http":
        command = ["curl", "-fsS", "-X", str(action_config.get("method", "GET")).upper()]
        for key, value in (action_config.get("headers") or {}).items():
            command.extend(["-H", f"{key}: {value}"])
        body = action_config.get("body")
        if body is not None:
            command.extend(
                ["-d", json.dumps(body) if isinstance(body, (dict, list)) else str(body)]
            )
        command.append(action_config["url"])

        return {
            "image": _HTTP_IMAGE,
            "command": command,
            "env": {},
            "timeout_seconds": int(action_config.get("timeout_seconds", 30)),
        }

    raise _unprocessable(f"Unsupported action_type: {action_type}")


def _resolve_schedule(raw_cron: str | None) -> tuple[str, str | None, datetime | None]:
    """Turn a cron expression into (job_type, cron_expression, next_fire_at).

    No cron means a one-time job: the scheduler ignores it entirely and it only
    runs when something calls /trigger (or when it is created/enabled).
    """
    cron = (raw_cron or "").strip() or None
    if cron is None:
        return "normal", None, None
    if not croniter.is_valid(cron):
        raise _unprocessable("Invalid cron expression")
    return "scheduled", cron, next_cron_time(cron, utcnow())


class JobService:
    def __init__(self, db: Session):
        self.db = db
        self.jobs = JobRepository(db)
        self.tasks = TaskRepository(db)

    # ── Commands ──────────────────────────────────────────────────────────────

    def create_job(self, payload: JobCreate) -> Job:
        job_type, cron, next_fire_at = _resolve_schedule(payload.cron_expression)

        job = Job(
            name=payload.name,
            job_type=job_type,
            cron_expression=cron,
            action_type=payload.action_type,
            action_config=payload.action_config,
            runtime_spec=_build_runtime_spec(payload.action_type, payload.action_config),
            enabled=payload.enabled,
            concurrency_policy=payload.concurrency_policy,
            max_retries=payload.max_retries,
            next_fire_at=next_fire_at,
        )
        if payload.upstream_job_ids:
            job.upstream_jobs.extend(self._jobs_by_id(payload.upstream_job_ids))
        if payload.downstream_job_ids:
            job.downstream_jobs.extend(self._jobs_by_id(payload.downstream_job_ids))

        self.db.add(job)
        self._assert_acyclic(job)
        job = self.jobs.create(job)

        self._dispatch_if_one_time(job)
        return job

    def update_job(self, job_id: str, payload: JobUpdate) -> Job:
        job = self.get_job(job_id)
        data = payload.model_dump(exclude_unset=True)
        was_enabled = job.enabled

        for key, value in data.items():
            if key not in ("cron_expression", "upstream_job_ids", "downstream_job_ids"):
                setattr(job, key, value)

        if "cron_expression" in data:
            job.job_type, job.cron_expression, job.next_fire_at = _resolve_schedule(
                data["cron_expression"]
            )

        # The runtime spec is derived state: re-derive it whenever either input moves.
        if "action_type" in data or "action_config" in data:
            action_type = data.get("action_type", job.action_type)
            action_config = data.get("action_config", job.action_config)
            schema = _ACTION_CONFIG_SCHEMAS.get(action_type)
            if schema is not None:
                schema.model_validate(action_config)
            job.runtime_spec = _build_runtime_spec(action_type, action_config)

        dependencies_changed = False
        if payload.upstream_job_ids is not None:
            job.upstream_jobs = self._jobs_by_id(payload.upstream_job_ids)
            dependencies_changed = True
        if payload.downstream_job_ids is not None:
            job.downstream_jobs = self._jobs_by_id(payload.downstream_job_ids)
            dependencies_changed = True
        if dependencies_changed:
            self._assert_acyclic(job)

        job = self.jobs.update(job)

        # Enabling a one-time job is what makes it run, mirroring create_job.
        if not was_enabled and job.enabled:
            self._dispatch_if_one_time(job)
        return job

    def delete_job(self, job_id: str) -> None:
        self.jobs.delete(self.get_job(job_id))

    def trigger_job(self, job_id: str, queue_client) -> Task:
        """Queue one on-demand run of a job."""
        job = self.get_job(job_id)
        return self._enqueue_task(job, queue_client)

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

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
        return self.jobs.list_paginated(
            page=page,
            page_size=page_size,
            enabled=enabled,
            action_type=action_type,
            concurrency_policy=concurrency_policy,
            q=q,
        )

    def list_job_tasks(self, job_id: str) -> list[Task]:
        self.get_job(job_id)
        return self.tasks.list_by_job(job_id)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _jobs_by_id(self, job_ids) -> list[Job]:
        return self.db.query(Job).filter(Job.id.in_(job_ids)).all()

    def _assert_acyclic(self, job: Job) -> None:
        """Reject a dependency edge that would make the job its own descendant."""
        # Flush so the recursive CTE sees the new edges; still uncommitted.
        self.db.flush()
        if str(job.id) in self.jobs.get_all_descendants(str(job.id)):
            raise _unprocessable("Circular dependency detected")

    def _dispatch_if_one_time(self, job: Job) -> None:
        """Run a one-time job right away, unless it is waiting on upstreams.

        Scheduled jobs are the scheduler's business; a one-time job has no other
        way to start.
        """
        if job.job_type == "normal" and job.enabled and not job.upstream_jobs:
            self._enqueue_task(job, get_normal_queue_client())

    def _enqueue_task(self, job: Job, queue_client) -> Task:
        # trigger_type stays "manual" for every on-demand run. "scheduled" is
        # reserved for the scheduler, and the dashboard's dispatch panels count it.
        task = self.tasks.create(
            Task(
                job_id=str(job.id),
                status="pending",
                trigger_type="manual",
                retry_count=0,
            )
        )
        queue_client.send_task(str(task.id))
        return task
