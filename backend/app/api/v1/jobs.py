from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.queue.factory import get_normal_queue_client
from app.schemas.job import (
    ActionType,
    ConcurrencyPolicy,
    JobCreate,
    JobListItem,
    JobListResponse,
    JobRead,
    JobUpdate,
    TriggerResponse,
)
from app.schemas.task import TaskRead
from app.services.job_service import JobService

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


def _service(db: Session = Depends(get_db)) -> JobService:
    return JobService(db)


@router.post("", response_model=JobRead)
def create_job(payload: JobCreate, service: JobService = Depends(_service)):
    return JobRead.model_validate(service.create_job(payload), from_attributes=True)


@router.get("", response_model=JobListResponse)
def list_jobs(
    service: JobService = Depends(_service),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    enabled: bool | None = Query(default=None),
    action_type: ActionType | None = Query(default=None),
    concurrency_policy: ConcurrencyPolicy | None = Query(default=None),
    q: str | None = Query(default=None, min_length=1, max_length=255),
):
    jobs, total = service.list_jobs(
        page=page,
        page_size=page_size,
        enabled=enabled,
        action_type=action_type,
        concurrency_policy=concurrency_policy,
        q=q,
    )
    return JobListResponse(
        items=[JobListItem.model_validate(job, from_attributes=True) for job in jobs],
        page=page,
        page_size=page_size,
        total=total,
        total_pages=max((total + page_size - 1) // page_size, 1),
    )


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: str, service: JobService = Depends(_service)):
    return JobRead.model_validate(service.get_job(job_id), from_attributes=True)


@router.put("/{job_id}", response_model=JobRead)
def update_job(
    job_id: str, payload: JobUpdate, service: JobService = Depends(_service)
):
    return JobRead.model_validate(
        service.update_job(job_id, payload), from_attributes=True
    )


@router.delete("/{job_id}")
def delete_job(job_id: str, service: JobService = Depends(_service)):
    service.delete_job(job_id)
    return {"ok": True}


@router.post("/{job_id}/trigger", response_model=TriggerResponse)
def trigger_job(job_id: str, service: JobService = Depends(_service)):
    """Run a job now. On-demand runs go to the normal queue."""
    task = service.trigger_job(job_id, get_normal_queue_client())
    return TriggerResponse(task_id=str(task.id), status=task.status)


@router.get("/{job_id}/tasks", response_model=list[TaskRead])
def list_job_tasks(job_id: str, service: JobService = Depends(_service)):
    return [
        TaskRead.model_validate(task, from_attributes=True)
        for task in service.list_job_tasks(job_id)
    ]
