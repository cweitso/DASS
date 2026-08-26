from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.task import Task
from app.queue.factory import get_retry_queue_client
from app.repositories.task_repository import TaskRepository
from app.schemas.task import RetryResponse, TaskRead

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


@router.get("/{task_id}", response_model=TaskRead)
def get_task(task_id: str, db: Session = Depends(get_db)):
    task = TaskRepository(db).get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskRead.model_validate(task, from_attributes=True)


@router.post("/{task_id}/retry", response_model=RetryResponse)
def retry_task(task_id: str, db: Session = Depends(get_db)):
    """Queue a fresh attempt of a failed task, carrying its retry count forward."""
    repo = TaskRepository(db)
    task = repo.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in ("failed", "final_failed"):
        raise HTTPException(status_code=409, detail="Only failed tasks can be retried")

    retry_task = repo.create(
        Task(
            job_id=task.job_id,
            status="pending",
            trigger_type=task.trigger_type,
            retry_count=task.retry_count + 1,
        )
    )
    get_retry_queue_client().send_task(str(retry_task.id))

    return RetryResponse(
        task_id=task.id,
        retry_task_id=retry_task.id,
        status=retry_task.status,
    )
