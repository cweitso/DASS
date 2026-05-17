from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.queue.factory import get_queue_client
from app.schemas.job import JobCreate, JobListItem, JobRead, JobUpdate, TriggerResponse
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


@router.get("", response_model=list[JobListItem])
def list_jobs(service: JobService = Depends(_service)):
    """列出所有 Job（精簡版欄位）。

    1. service.list_jobs()
    2. 用 JobListItem.model_validate 轉換每一筆
    """

    jobs = service.list_jobs()

    return [JobListItem.model_validate(job, from_attributes=True) for job in jobs]


@router.get("/{job_id}", response_model=JobRead)
def get_job(
    job_id: str,
    service: JobService = Depends(_service),
):
    """取得單一 Job 詳細資料。

    # service.get_job → JobRead.model_validate
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

    # TODO: service.update_job → JobRead.model_validate
    """

    job = service.update_job(job_id, payload)

    return JobRead.model_validate(job, from_attributes=True)


@router.delete("/{job_id}")
def delete_job(
    job_id: str,
    service: JobService = Depends(_service),
):
    """刪除 Job。

    # service.delete_job → return {"ok": True}
    """

    service.delete_job(job_id)

    return {"ok": True}


@router.post("/{job_id}/trigger", response_model=TriggerResponse)
def trigger_job(
    job_id: str,
    service: JobService = Depends(_service),
):
    """手動觸發 Job 執行。

    1. queue = get_queue_client()
    2. task = service.trigger_job(job_id, queue)
    3. 回傳 TriggerResponse(task_id=str(task.id), status=task.status)
    """

    queue = get_queue_client()
    task = service.trigger_job(job_id, queue)

    return TriggerResponse(task_id=str(task.id), status=task.status)


@router.get("/{job_id}/tasks", response_model=list[TaskRead])
def list_job_tasks(
    job_id: str,
    service: JobService = Depends(_service),
):
    """列出指定 Job 的 Task 歷史。

    # service.list_job_tasks → 轉 TaskRead
    """

    tasks = service.list_job_tasks(job_id)

    return [TaskRead.model_validate(task, from_attributes=True) for task in tasks]
