from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.job import Job
from app.models.task import Task
from app.repositories.job_repository import JobRepository
from app.repositories.task_repository import TaskRepository


@pytest.fixture
def job_repo(db_session):
    return JobRepository(db_session)


@pytest.fixture
def task_repo(db_session):
    return TaskRepository(db_session)


class TestJobRepository:
    """Tests for JobRepository CRUD operations."""

    def test_create_job(self, job_repo, db_session):
        job = job_repo.create(
            name="test-job",
            cron_expression="* * * * *",
            action_type="http",
            action_config={"method": "GET", "url": "https://example.com"},
            enabled=True,
            concurrency_policy="allow",
            max_retries=1,
        )
        assert job.id is not None
        assert job.name == "test-job"
        assert db_session.query(Job).filter(Job.id == job.id).first() is not None

    def test_get_job(self, job_repo, db_session):
        job = job_repo.create(
            name="get-test",
            cron_expression="0 0 * * *",
            action_type="shell",
            action_config={"command": "echo hi"},
            enabled=True,
            concurrency_policy="forbid",
            max_retries=0,
        )
        retrieved = job_repo.get(job.id)
        assert retrieved.name == "get-test"

    def test_list_jobs(self, job_repo, db_session):
        job_repo.create(
            name="job-1",
            cron_expression="* * * * *",
            action_type="http",
            action_config={"method": "GET", "url": "https://a.com"},
            enabled=True,
            concurrency_policy="allow",
            max_retries=0,
        )
        job_repo.create(
            name="job-2",
            cron_expression="*/5 * * * *",
            action_type="http",
            action_config={"method": "POST", "url": "https://b.com"},
            enabled=False,
            concurrency_policy="allow",
            max_retries=1,
        )
        jobs = job_repo.list()
        assert len(jobs) == 2
        assert any(j.name == "job-1" for j in jobs)
        assert any(j.name == "job-2" for j in jobs)

    def test_update_job(self, job_repo, db_session):
        job = job_repo.create(
            name="old-name",
            cron_expression="* * * * *",
            action_type="http",
            action_config={"method": "GET", "url": "https://example.com"},
            enabled=True,
            concurrency_policy="allow",
            max_retries=0,
        )
        job_repo.update(job.id, name="new-name", enabled=False)
        updated = job_repo.get(job.id)
        assert updated.name == "new-name"
        assert updated.enabled is False

    def test_delete_job(self, job_repo, db_session):
        job = job_repo.create(
            name="to-delete",
            cron_expression="* * * * *",
            action_type="http",
            action_config={"method": "GET", "url": "https://example.com"},
            enabled=True,
            concurrency_policy="allow",
            max_retries=0,
        )
        job_id = job.id
        job_repo.delete(job_id)
        retrieved = job_repo.get(job_id)
        assert retrieved is None

    def test_due_jobs_returns_past_next_fire_at(self, job_repo, db_session):
        now = datetime.now(UTC)
        job = job_repo.create(
            name="due-job",
            cron_expression="* * * * *",
            action_type="http",
            action_config={"method": "GET", "url": "https://example.com"},
            enabled=True,
            concurrency_policy="allow",
            max_retries=0,
            next_fire_at=now - timedelta(seconds=1),
        )
        due = job_repo.due_jobs(now)
        assert len(due) > 0
        assert any(j.id == job.id for j in due)


class TestTaskRepository:
    """Tests for TaskRepository CRUD operations."""

    def test_create_task(self, task_repo, db_session):
        job = db_session.query(Job).first() or Job(
            id=str(uuid4()),
            name="parent-job",
            cron_expression="* * * * *",
            action_type="http",
            action_config={"method": "GET", "url": "https://example.com"},
            enabled=True,
            concurrency_policy="allow",
            max_retries=0,
        )
        if not job.id or not db_session.query(Job).filter(Job.id == job.id).first():
            db_session.add(job)
            db_session.commit()

        task = task_repo.create(job.id, "manual")
        assert task.id is not None
        assert task.job_id == job.id
        assert task.status == "pending"

    def test_get_task(self, task_repo, db_session):
        job = db_session.query(Job).first() or Job(
            id=str(uuid4()),
            name="parent-job",
            cron_expression="* * * * *",
            action_type="http",
            action_config={"method": "GET", "url": "https://example.com"},
            enabled=True,
            concurrency_policy="allow",
            max_retries=0,
        )
        if not job.id or not db_session.query(Job).filter(Job.id == job.id).first():
            db_session.add(job)
            db_session.commit()

        task = task_repo.create(job.id, "manual")
        retrieved = task_repo.get(task.id)
        assert retrieved.id == task.id
        assert retrieved.job_id == job.id

    def test_list_by_job(self, task_repo, db_session):
        job = Job(
            id=str(uuid4()),
            name="multi-task-job",
            cron_expression="* * * * *",
            action_type="http",
            action_config={"method": "GET", "url": "https://example.com"},
            enabled=True,
            concurrency_policy="allow",
            max_retries=0,
        )
        db_session.add(job)
        db_session.commit()

        task1 = task_repo.create(job.id, "manual")
        task2 = task_repo.create(job.id, "scheduled")
        tasks = task_repo.list_by_job(job.id)
        assert len(tasks) == 2
        assert any(t.id == task1.id for t in tasks)
        assert any(t.id == task2.id for t in tasks)

    def test_count_running_for_job(self, task_repo, db_session):
        job = Job(
            id=str(uuid4()),
            name="count-job",
            cron_expression="* * * * *",
            action_type="http",
            action_config={"method": "GET", "url": "https://example.com"},
            enabled=True,
            concurrency_policy="forbid",
            max_retries=0,
        )
        db_session.add(job)
        db_session.commit()

        task_repo.create(job.id, "manual")
        task = task_repo.create(job.id, "scheduled")
        db_session.execute(
            f"UPDATE tasks SET status='running' WHERE id='{task.id}'"
        )
        db_session.commit()

        count = task_repo.count_running_for_job(job.id)
        assert count == 1
