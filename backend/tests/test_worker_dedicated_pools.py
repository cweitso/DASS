"""Integration tests for the dedicated-pool-per-queue architecture.

Each of the three queues (normal, scheduled, retry) gets its own worker pool.
These tests verify:
  1. A pool only consumes from its own queue — other queues are untouched.
  2. All three pools can process tasks independently without interfering.
  3. Failed tasks with retries remaining are routed to the retry queue, not the
     normal or scheduled queues.
  4. The retry pool can pick up and process those retry tasks.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.models.job import Job
from app.models.task import Task
from app.queue.memory import MemoryQueueClient
from app.services.execution_service import ExecutionResult
from app.services.worker_service import WorkerService
from app.utils.time import utcnow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_job(db_session, *, max_retries: int = 0) -> Job:
    job = Job(
        name=f"pool-test-{uuid4()}",
        cron_expression="* * * * *",
        action_type="container",
        action_config={},
        runtime_spec={
            "image": "alpine:latest",
            "command": ["true"],
            "timeout_seconds": 5,
        },
        enabled=True,
        concurrency_policy="allow",
        max_retries=max_retries,
        next_fire_at=utcnow(),
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def _seed_task(db_session, job: Job, *, retry_count: int = 0) -> Task:
    task = Task(
        job_id=job.id,
        status="pending",
        trigger_type="manual",
        retry_count=retry_count,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def _drain_queue(db_session, queue: MemoryQueueClient, retry_queue: MemoryQueueClient) -> list[str]:
    """Pull all ready messages from *queue* and process them via WorkerService."""
    worker = WorkerService(
        db=db_session,
        queue_client=queue,
        worker_id="test-pool-worker",
        retry_queue=retry_queue,
    )
    messages = queue.receive_tasks(max_messages=10, wait_time_seconds=0)
    processed = []
    for msg in messages:
        task_id = json.loads(msg.body)["task_id"]
        worker.process_task_id(task_id)
        queue.delete_message(msg.receipt_handle)
        processed.append(task_id)
    return processed


class _SuccessExecutor:
    def run(self, *args, **kwargs):
        return ExecutionResult(success=True, stdout="ok", stderr="")


class _FailingExecutor:
    def run(self, *args, **kwargs):
        return ExecutionResult(success=False, stdout="", stderr="boom")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDedicatedPoolQueueIsolation:
    """Verify that each pool only consumes its own queue."""

    def test_normal_pool_does_not_drain_scheduled_queue(self, db_session):
        """Processing normal queue must leave scheduled queue untouched."""
        normal_q = MemoryQueueClient()
        scheduled_q = MemoryQueueClient()
        retry_q = MemoryQueueClient()

        job = _seed_job(db_session)
        normal_task = _seed_task(db_session, job)
        scheduled_task = _seed_task(db_session, job)

        normal_q.send_task(str(normal_task.id))
        scheduled_q.send_task(str(scheduled_task.id))

        # Simulate normal pool: drain only normal_q
        _drain_queue(db_session, normal_q, retry_q)

        # normal_q is now empty
        assert normal_q.receive_tasks(max_messages=5, wait_time_seconds=0) == []

        # scheduled_q still holds its message
        remaining = scheduled_q.receive_tasks(max_messages=5, wait_time_seconds=0)
        assert len(remaining) == 1
        assert json.loads(remaining[0].body)["task_id"] == str(scheduled_task.id)

    def test_scheduled_pool_does_not_drain_normal_queue(self, db_session):
        """Processing scheduled queue must leave normal queue untouched."""
        normal_q = MemoryQueueClient()
        scheduled_q = MemoryQueueClient()
        retry_q = MemoryQueueClient()

        job = _seed_job(db_session)
        normal_task = _seed_task(db_session, job)
        scheduled_task = _seed_task(db_session, job)

        normal_q.send_task(str(normal_task.id))
        scheduled_q.send_task(str(scheduled_task.id))

        # Simulate scheduled pool: drain only scheduled_q
        _drain_queue(db_session, scheduled_q, retry_q)

        assert scheduled_q.receive_tasks(max_messages=5, wait_time_seconds=0) == []

        remaining = normal_q.receive_tasks(max_messages=5, wait_time_seconds=0)
        assert len(remaining) == 1
        assert json.loads(remaining[0].body)["task_id"] == str(normal_task.id)

    def test_retry_pool_does_not_drain_normal_or_scheduled_queues(self, db_session):
        """Processing retry queue must leave normal and scheduled queues untouched."""
        normal_q = MemoryQueueClient()
        scheduled_q = MemoryQueueClient()
        retry_q = MemoryQueueClient()

        job = _seed_job(db_session)
        normal_task = _seed_task(db_session, job)
        scheduled_task = _seed_task(db_session, job)
        retry_task = _seed_task(db_session, job, retry_count=1)

        normal_q.send_task(str(normal_task.id))
        scheduled_q.send_task(str(scheduled_task.id))
        retry_q.send_task(str(retry_task.id))

        # Simulate retry pool: drain only retry_q
        _drain_queue(db_session, retry_q, retry_q)

        assert retry_q.receive_tasks(max_messages=5, wait_time_seconds=0) == []

        # Other queues untouched
        assert len(normal_q.receive_tasks(max_messages=5, wait_time_seconds=0)) == 1
        assert len(scheduled_q.receive_tasks(max_messages=5, wait_time_seconds=0)) == 1


class TestThreePoolsProcessIndependently:
    """All three queues are fully processed without cross-queue interference."""

    def test_all_three_queues_processed_independently(self, db_session):
        """Each pool drains its own queue; all tasks reach a terminal state."""
        normal_q = MemoryQueueClient()
        scheduled_q = MemoryQueueClient()
        retry_q = MemoryQueueClient()

        job = _seed_job(db_session)
        normal_task = _seed_task(db_session, job)
        scheduled_task = _seed_task(db_session, job)
        retry_task = _seed_task(db_session, job, retry_count=1)

        normal_q.send_task(str(normal_task.id))
        scheduled_q.send_task(str(scheduled_task.id))
        retry_q.send_task(str(retry_task.id))

        # Each pool processes its own queue independently
        normal_worker = WorkerService(db=db_session, queue_client=normal_q, worker_id="pool-normal", retry_queue=retry_q)
        scheduled_worker = WorkerService(db=db_session, queue_client=scheduled_q, worker_id="pool-scheduled", retry_queue=retry_q)
        retry_worker = WorkerService(db=db_session, queue_client=retry_q, worker_id="pool-retry", retry_queue=retry_q)

        for worker, queue in [
            (normal_worker, normal_q),
            (scheduled_worker, scheduled_q),
            (retry_worker, retry_q),
        ]:
            worker.executor = _SuccessExecutor()
            msgs = queue.receive_tasks(max_messages=5, wait_time_seconds=0)
            for msg in msgs:
                task_id = json.loads(msg.body)["task_id"]
                worker.process_task_id(task_id)
                queue.delete_message(msg.receipt_handle)

        # All queues now empty
        for q in (normal_q, scheduled_q, retry_q):
            assert q.receive_tasks(max_messages=5, wait_time_seconds=0) == []

        # All tasks in terminal state
        db_session.expire_all()
        for task in (normal_task, scheduled_task, retry_task):
            status = db_session.get(Task, task.id).status
            assert status in ("success", "final_failed"), f"task {task.id} stuck at {status}"


class TestRetryRouting:
    """Failed tasks must land in the retry queue, not back in normal/scheduled."""

    def test_failed_task_goes_to_retry_queue_not_normal_queue(self, db_session):
        """On failure with retries remaining, the new task is enqueued only to retry_q."""
        normal_q = MemoryQueueClient()
        scheduled_q = MemoryQueueClient()
        retry_q = MemoryQueueClient()

        job = _seed_job(db_session, max_retries=1)
        task = _seed_task(db_session, job)
        normal_q.send_task(str(task.id))

        worker = WorkerService(
            db=db_session,
            queue_client=normal_q,
            worker_id="pool-normal",
            retry_queue=retry_q,
        )
        worker.executor = _FailingExecutor()

        msgs = normal_q.receive_tasks(max_messages=1, wait_time_seconds=0)
        task_id = json.loads(msgs[0].body)["task_id"]
        worker.process_task_id(task_id)
        normal_q.delete_message(msgs[0].receipt_handle)

        # retry_q received the retry task
        retry_msgs = retry_q.receive_tasks(max_messages=5, wait_time_seconds=0)
        assert len(retry_msgs) == 1, "retry task should be in retry_q"

        # normal_q and scheduled_q are clean
        assert normal_q.receive_tasks(max_messages=5, wait_time_seconds=0) == []
        assert scheduled_q.receive_tasks(max_messages=5, wait_time_seconds=0) == []

        # Verify the retry task in DB
        db_session.expire_all()
        tasks = db_session.query(Task).filter(Task.job_id == job.id).all()
        assert len(tasks) == 2
        retry_task = max(tasks, key=lambda t: t.retry_count)
        assert retry_task.retry_count == 1
        assert retry_task.status == "pending"

    def test_retry_pool_can_process_retry_queue_task(self, db_session):
        """The retry pool successfully processes a task that arrived via retry_q."""
        retry_q = MemoryQueueClient()

        job = _seed_job(db_session, max_retries=1)
        # Simulate a task already on its first retry
        retry_task = _seed_task(db_session, job, retry_count=1)
        retry_q.send_task(str(retry_task.id))

        worker = WorkerService(
            db=db_session,
            queue_client=retry_q,
            worker_id="pool-retry",
            retry_queue=retry_q,
        )
        worker.executor = _SuccessExecutor()

        msgs = retry_q.receive_tasks(max_messages=1, wait_time_seconds=0)
        task_id = json.loads(msgs[0].body)["task_id"]
        result = worker.process_task_id(task_id)
        assert result is True
        retry_q.delete_message(msgs[0].receipt_handle)

        db_session.expire_all()
        assert db_session.get(Task, retry_task.id).status == "success"
