from __future__ import annotations

import concurrent.futures
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

    def test_due_jobs_filters_by_enabled_and_timing(self, job_repo, db_session):
        """due_jobs 只能撈到 enabled=True 且 next_fire_at <= now 的 job。"""
        now = datetime.now(UTC)

        # 1. Happy path: 已到期 + enabled → 抓得到
        job_normal = _make_job(name="due-job", next_fire_at=now - timedelta(minutes=1))
        # 2. Sad path: 雖然到期但被停用 → 不該抓到
        job_disabled = _make_job(
            name="disabled-job",
            enabled=False,
            next_fire_at=now - timedelta(minutes=1),
        )
        # 3. Edge: 尚未到期 → 不該抓到
        job_future = _make_job(
            name="future-job",
            next_fire_at=now + timedelta(minutes=1),
        )
        # 4. Edge: 剛好現在這一秒（<= 應該抓到）
        job_exact_now = _make_job(name="exact-now-job", next_fire_at=now)

        db_session.add_all([job_normal, job_disabled, job_future, job_exact_now])
        db_session.commit()

        due = job_repo.due_jobs(now)
        due_ids = {j.id for j in due}

        assert len(due) == 2
        assert job_normal.id in due_ids
        assert job_exact_now.id in due_ids


class TestTaskRepository:
    """Tests for TaskRepository CRUD + claim/orphan/mark_failed semantics."""

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

    def test_claim_pending_success(self, task_repo, db_session):
        """pending task 被 worker 原子搶單後，狀態變 running 且 locked_by 寫入。"""
        job = self._seed_job(db_session)
        task = task_repo.create(_make_task(job.id, trigger_type="scheduled"))

        locked_until = datetime.now(UTC) + timedelta(minutes=5)
        claimed = task_repo.claim_pending(str(task.id), "worker-999", locked_until)

        assert claimed is not None
        assert claimed.status == "running"
        assert claimed.locked_by == "worker-999"

    def test_claim_pending_fails_when_already_claimed(self, task_repo, db_session):
        """task 已經是 running 時，第二個 worker 搶不到，應回 None。"""
        job = self._seed_job(db_session)
        task = _make_task(
            job.id,
            status="running",
            locked_by="worker-111",
            trigger_type="scheduled",
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        locked_until = datetime.now(UTC) + timedelta(minutes=5)
        claimed = task_repo.claim_pending(str(task.id), "worker-999", locked_until)

        assert claimed is None

    def test_claim_pending_concurrency_race(self, task_repo, db_session):
        """5 個 worker 同時搶同一張 pending task，必須剛好只有 1 個搶到。"""
        job = self._seed_job(db_session)
        task = _make_task(job.id, trigger_type="scheduled")
        db_session.add(task)
        db_session.commit()
        task_id = str(task.id)
        locked_until = datetime.now(UTC) + timedelta(minutes=5)

        def worker_rush(worker_name):
            try:
                return task_repo.claim_pending(task_id, worker_name, locked_until)
            except Exception:
                return None

        worker_names = ["worker-A", "worker-B", "worker-C", "worker-D", "worker-E"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(worker_rush, worker_names))

        success_claims = [r for r in results if r is not None]
        assert len(success_claims) == 1
        assert success_claims[0].status == "running"
        assert success_claims[0].locked_by in worker_names

    def test_list_expired_running_edge_cases(self, task_repo, db_session):
        """orphan 清道夫只能撈到 status=running 且 locked_until < now 的 task。"""
        job = self._seed_job(db_session)
        now = datetime.now(UTC)

        # 過期 1 秒 → 應抓
        expired = _make_task(
            job.id,
            status="running",
            locked_until=now - timedelta(seconds=1),
            trigger_type="scheduled",
        )
        # 還有 1 秒才過期 → 不該抓
        alive = _make_task(
            job.id,
            status="running",
            locked_until=now + timedelta(seconds=1),
            trigger_type="scheduled",
        )
        # 已 success 但鎖時間殘留 → 不該抓
        finished = _make_task(
            job.id,
            status="success",
            locked_until=now - timedelta(days=1),
            trigger_type="scheduled",
        )
        # locked_until == now（嚴格 <），不該抓
        exact_zero = _make_task(
            job.id,
            status="running",
            locked_until=now,
            trigger_type="scheduled",
        )
        db_session.add_all([expired, alive, finished, exact_zero])
        db_session.commit()

        orphans = task_repo.list_expired_running(now)

        assert len(orphans) == 1
        assert orphans[0].id == expired.id

    def test_mark_failed_clears_lock_and_records_output(self, task_repo, db_session):
        """final=False：status→failed，locked_by/until 清空，stderr 寫入。"""
        job = self._seed_job(db_session)
        task = _make_task(
            job.id,
            status="running",
            locked_by="worker-1",
            locked_until=datetime.now(UTC) + timedelta(seconds=30),
            trigger_type="manual",
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        updated = task_repo.mark_failed(task, stdout="some log", stderr="error trace", final=False)

        assert updated.status == "failed"
        assert updated.locked_by is None
        assert updated.locked_until is None
        assert updated.finished_at is not None
        assert updated.stderr == "error trace"

    def test_mark_failed_final_sets_terminal_status(self, task_repo, db_session):
        """final=True：status→final_failed，並同樣清空鎖。"""
        job = self._seed_job(db_session)
        task = _make_task(
            job.id,
            status="running",
            locked_by="worker-1",
            locked_until=datetime.now(UTC) + timedelta(seconds=30),
            trigger_type="manual",
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        updated = task_repo.mark_failed(task, stdout="logs", stderr="fatal error", final=True)

        assert updated.status == "final_failed"
        assert updated.locked_by is None
        assert updated.locked_until is None
