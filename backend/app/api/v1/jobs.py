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
def create_job(
    payload: JobCreate,
    service: JobService = Depends(_service),
):
    """建立新 Job。

    1. service.create_job(payload)
    2. 用 JobRead.model_validate(job, from_attributes=True) 轉換回傳
    """

    job = service.create_job(payload)

    return JobRead.model_validate(job, from_attributes=True)


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
    """列出 Job，支援分頁與篩選。

    1. service.list_jobs(...)
    2. 用 JobListItem.model_validate 轉換每一筆
    3. 回傳分頁資訊
    """

    jobs, total = service.list_jobs(
        page=page,
        page_size=page_size,
        enabled=enabled,
        action_type=action_type,
        concurrency_policy=concurrency_policy,
        q=q,
    )
    items = [JobListItem.model_validate(job, from_attributes=True) for job in jobs]
    total_pages = max((total + page_size - 1) // page_size, 1)

    return JobListResponse(
        items=items,
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
    )


@router.get("/{job_id}", response_model=JobRead)
def get_job(
    job_id: str,
    service: JobService = Depends(_service),
):
    """取得單一 Job 詳細資料。

    service.get_job → JobRead.model_validate
    """

    job = service.get_job(job_id)

    return JobRead.model_validate(job, from_attributes=True)


@router.put("/{job_id}", response_model=JobRead)
def update_job(
    job_id: str,
    payload: JobUpdate,
    service: JobService = Depends(_service),
):
    """更新 Job 欄位。

    service.update_job → JobRead.model_validate
    """

    job = service.update_job(job_id, payload)

    return JobRead.model_validate(job, from_attributes=True)


@router.delete("/{job_id}")
def delete_job(
    job_id: str,
    service: JobService = Depends(_service),
):
    """刪除 Job。

    service.delete_job → return {"ok": True}
    """

    service.delete_job(job_id)

    return {"ok": True}


@router.post("/{job_id}/trigger", response_model=TriggerResponse)
def trigger_job(
    job_id: str,
    service: JobService = Depends(_service),
):
    """手動觸發 Job 執行。

    1. queue = get_normal_queue_client()  # S4: API immediate trigger → normal queue
    2. task = service.trigger_job(job_id, queue)
    3. 回傳 TriggerResponse(task_id=str(task.id), status=task.status)
    """

    queue = get_normal_queue_client()
    task = service.trigger_job(job_id, queue)

    return TriggerResponse(task_id=str(task.id), status=task.status)


@router.get("/{job_id}/tasks", response_model=list[TaskRead])
def list_job_tasks(
    job_id: str,
    service: JobService = Depends(_service),
):
    """列出指定 Job 的 Task 歷史。

    service.list_job_tasks → 轉 TaskRead
    """

    tasks = service.list_job_tasks(job_id)

    return [TaskRead.model_validate(task, from_attributes=True) for task in tasks]
