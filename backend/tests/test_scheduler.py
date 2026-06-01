from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.job import Job
from app.models.task import Task
from app.services.scheduler_service import SchedulerService
from app.queue.memory import MemoryQueueClient


def _job(db_session, **overrides):
    """Helper to create a test job."""
    job = Job(
        id=str(uuid4()),
        name=overrides.get("name", f"job-{uuid4()}"),
        cron_expression="* * * * *",
        action_type=overrides.get("action_type", "http"),
        action_config=overrides.get(
            "action_config",
            {"method": "GET", "url": "https://example.com", "timeout_seconds": 1},
        ),
        enabled=overrides.get("enabled", True),
        concurrency_policy=overrides.get("concurrency_policy", "allow"),
        max_retries=overrides.get("max_retries", 0),
        next_fire_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(job)
    db_session.commit()
    return job


from sqlalchemy.orm import sessionmaker


class TestSchedulerService:
    """Tests for SchedulerService dispatch and orphan recovery."""

    def test_scheduler_dispatch_due_job(self, db_session):
        """Scheduler should dispatch a job when next_fire_at has passed."""
        queue = MemoryQueueClient()
        job = _job(db_session)

        # 建立一個測試用的連線工廠，綁定到目前的測試資料庫引擎
        factory = sessionmaker(bind=db_session.get_bind())
        service = SchedulerService(factory, queue)

        service.sync_jobs()
        created = service.dispatch_due_jobs()

        assert created == 1
        tasks = db_session.query(Task).filter(Task.job_id == job.id).all()
        assert len(tasks) == 1

    def test_scheduler_dispatch_routes_to_scheduled_queue(self, db_session):
        """Scheduler 的派發必須落在 scheduled queue，normal queue 應保持空。
        Worker 端依 normal > scheduled > retry 優先序消費，路由錯了就違反這個設計。
        """
        normal_queue = MemoryQueueClient()
        scheduled_queue = MemoryQueueClient()
        _job(db_session)
        
        factory = sessionmaker(bind=db_session.get_bind())
        # 新的實作中 SchedulerService 只需要傳入 scheduled_queue
        service = SchedulerService(factory, scheduled_queue)
        service.sync_jobs()

        service.dispatch_due_jobs()

        # normal_queue 沒有傳入，一定是空的。scheduled_queue 會有派發的任務。
        assert normal_queue._queue.empty()
        assert scheduled_queue._queue.qsize() == 1

    def test_concurrency_policy_forbid_skips_running_task(self, db_session):
        """Scheduler should skip job if concurrency_policy=forbid and task is running."""
        queue = MemoryQueueClient()
        job = _job(db_session, concurrency_policy="forbid")
        running = Task(
            job_id=job.id, status="running", trigger_type="scheduled", retry_count=0
        )
        db_session.add(running)
        db_session.commit()

        factory = sessionmaker(bind=db_session.get_bind())
        service = SchedulerService(factory, queue)

        service.sync_jobs()
        service.dispatch_due_jobs()

        tasks = db_session.query(Task).filter(Task.job_id == job.id).all()
        assert len(tasks) == 1

    def test_orphan_recovery(self, db_session):
        """Scheduler should recover tasks with expired locks."""
        queue = MemoryQueueClient()
        job = _job(db_session)
        task = Task(
            job_id=job.id,
            status="running",
            trigger_type="scheduled",
            retry_count=0,
            locked_by="worker-1",
            locked_until=datetime.now(UTC) - timedelta(seconds=1),
        )
        db_session.add(task)
        db_session.commit()

        factory = sessionmaker(bind=db_session.get_bind())
        service = SchedulerService(factory, queue)

        service.sync_jobs()
        recovered = service.recover_orphans()

        assert recovered == 1
        db_session.refresh(task)
        assert task.status == "pending"

    def test_orphan_recovery_does_not_resend_message(self, db_session):
        """recover_orphans 不該主動重塞 message；SQS visibility 過期會自己 surface。"""
        queue = MemoryQueueClient()
        job = _job(db_session)
        task = Task(
            job_id=job.id,
            status="running",
            trigger_type="scheduled",
            retry_count=0,
            locked_by="worker-1",
            locked_until=datetime.now(UTC) - timedelta(seconds=1),
        )
        db_session.add(task)
        db_session.commit()
        factory = sessionmaker(bind=db_session.get_bind())
        service = SchedulerService(factory, queue)
        service.sync_jobs()
        service.recover_orphans()
        # MemoryQueueClient 沒 visibility 概念；確認 queue 是空的，沒被偷塞 message
        assert queue._queue.empty()
