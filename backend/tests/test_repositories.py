from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.models.job import Job
from app.models.task import Task
from app.repositories.job_repository import JobRepository
from app.repositories.task_repository import TaskRepository


def _make_job(**overrides) -> Job:
    """Build a Job with sensible defaults; mutate fields via overrides.

    Always supplies next_fire_at (Job.next_fire_at is NOT NULL).
    """
    kwargs = {
        "name": f"job-{uuid4()}",
        "cron_expression": "* * * * *",
        "action_type": "http",
        "action_config": {"method": "GET", "url": "https://example.com"},
        "enabled": True,
        "concurrency_policy": "allow",
        "max_retries": 0,
        "next_fire_at": datetime.now(UTC),
    }
    kwargs.update(overrides)
    return Job(**kwargs)


def _make_task(job_id: str, **overrides) -> Task:
    kwargs = {
        "job_id": job_id,
        "status": "pending",
        "trigger_type": "manual",
        "retry_count": 0,
    }
    kwargs.update(overrides)
    return Task(**kwargs)


@pytest.fixture
def job_repo(db_session):
    return JobRepository(db_session)


@pytest.fixture
def task_repo(db_session):
    return TaskRepository(db_session)


class TestJobRepository:
    """Tests for JobRepository CRUD — calls repo with Job model objects per stub TODOs."""

    def test_create_job(self, job_repo, db_session):
        job = _make_job(name="test-job", max_retries=1)
        created = job_repo.create(job)
        assert created.id is not None
        assert created.name == "test-job"
        assert db_session.query(Job).filter(Job.id == created.id).first() is not None

    def test_get_job(self, job_repo):
        job = _make_job(
            name="get-test",
            cron_expression="0 0 * * *",
            action_type="shell",
            action_config={"command": "echo hi"},
            concurrency_policy="forbid",
        )
        created = job_repo.create(job)
        retrieved = job_repo.get(created.id)
        assert retrieved is not None
        assert retrieved.name == "get-test"

    def test_list_jobs(self, job_repo):
        job_repo.create(_make_job(name="job-1"))
        job_repo.create(_make_job(name="job-2", enabled=False, max_retries=1))
        jobs = job_repo.list()
        assert len(jobs) == 2
        assert {j.name for j in jobs} == {"job-1", "job-2"}

    def test_update_job(self, job_repo):
        created = job_repo.create(_make_job(name="old-name"))
        created.name = "new-name"
        created.enabled = False
        job_repo.update(created)
        retrieved = job_repo.get(created.id)
        assert retrieved.name == "new-name"
        assert retrieved.enabled is False

    def test_delete_job(self, job_repo):
        created = job_repo.create(_make_job(name="to-delete"))
        job_id = created.id
        job_repo.delete(created)
        assert job_repo.get(job_id) is None

    def test_due_jobs_returns_past_next_fire_at(self, job_repo):
        now = datetime.now(UTC)
        created = job_repo.create(
            _make_job(name="due-job", next_fire_at=now - timedelta(seconds=1))
        )
        due = job_repo.due_jobs(now)
        assert any(j.id == created.id for j in due)


class TestTaskRepository:
    """Tests for TaskRepository CRUD — calls repo with Task model objects per stub TODOs."""

    def _seed_job(self, db_session) -> Job:
        """Insert a Job directly via session for FK setup (don't go through repo)."""
        job = _make_job()
        db_session.add(job)
        db_session.commit()
        return job

    def test_create_task(self, task_repo, db_session):
        job = self._seed_job(db_session)
        created = task_repo.create(_make_task(job.id, trigger_type="manual"))
        assert created.id is not None
        assert created.job_id == job.id
        assert created.status == "pending"

    def test_get_task(self, task_repo, db_session):
        job = self._seed_job(db_session)
        created = task_repo.create(_make_task(job.id, trigger_type="manual"))
        retrieved = task_repo.get(created.id)
        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.job_id == job.id

    def test_list_by_job(self, task_repo, db_session):
        job = self._seed_job(db_session)
        t1 = task_repo.create(_make_task(job.id, trigger_type="manual"))
        t2 = task_repo.create(_make_task(job.id, trigger_type="scheduled"))
        tasks = task_repo.list_by_job(job.id)
        assert {t.id for t in tasks} == {t1.id, t2.id}

    def test_count_running_for_job(self, task_repo, db_session):
        job = self._seed_job(db_session)
        task_repo.create(_make_task(job.id, trigger_type="manual"))
        running = task_repo.create(_make_task(job.id, trigger_type="scheduled"))
        db_session.execute(
            text("UPDATE tasks SET status='running' WHERE id=:tid"),
            {"tid": str(running.id)},
        )
        db_session.commit()
        assert task_repo.count_running_for_job(job.id) == 1
