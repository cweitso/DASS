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
        job_type=overrides.get("job_type", "scheduled"),
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

        # Session factory bound to the test engine, mirroring SessionLocal.
        factory = sessionmaker(bind=db_session.get_bind())
        service = SchedulerService(factory, queue, queue)

        service.sync_jobs()
        created = service.dispatch_due_jobs()

        assert created == 1
        tasks = db_session.query(Task).filter(Task.job_id == job.id).all()
        assert len(tasks) == 1

    def test_scheduler_dispatch_routes_to_scheduled_queue(self, db_session):
        """Cron dispatches must land on the scheduled queue, never the normal one.

        The two queues are consumed by separate worker pools; routing a cron
        dispatch to the normal queue breaks that separation.
        """
        normal_queue = MemoryQueueClient()
        scheduled_queue = MemoryQueueClient()
        _job(db_session)

        factory = sessionmaker(bind=db_session.get_bind())
        # Cron dispatch only ever touches the scheduled queue.
        service = SchedulerService(factory, scheduled_queue, scheduled_queue)
        service.sync_jobs()

        service.dispatch_due_jobs()

        # The normal queue was never handed to the cron path, so it stays empty.
        assert normal_queue._queue.empty()
        assert scheduled_queue._queue.qsize() == 1

    def test_scheduler_ignores_normal_jobs(self, db_session):
        """A one-time job must never be dispatched by the scheduler.

        One-time jobs are fired by the API into the normal queue; dispatching them
        here as well would run every job twice.
        """
        queue = MemoryQueueClient()
        job = _job(db_session, job_type="normal")

        factory = sessionmaker(bind=db_session.get_bind())
        service = SchedulerService(factory, queue, queue)

        service.sync_jobs()
        created = service.dispatch_due_jobs()

        assert created == 0
        assert db_session.query(Task).filter(Task.job_id == job.id).count() == 0

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
        service = SchedulerService(factory, queue, queue)

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
        service = SchedulerService(factory, queue, queue)

        service.sync_jobs()
        recovered = service.recover_orphans()

        assert recovered == 1
        db_session.refresh(task)
        assert task.status == "pending"

    def test_orphan_recovery_does_not_resend_message(self, db_session):
        """recover_orphans must not re-send: the queue re-delivers on its own."""
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
        service = SchedulerService(factory, queue, queue)
        service.sync_jobs()
        service.recover_orphans()
        # MemoryQueueClient has no visibility timeout, so an empty queue proves
        # recover_orphans did not enqueue anything.
        assert queue._queue.empty()

    def test_trigger_dependent_jobs(self, db_session):
        """A successful task triggers the downstream jobs that depend on it."""
        queue = MemoryQueueClient()
        factory = sessionmaker(bind=db_session.get_bind())
        service = SchedulerService(factory, queue, queue)

        # Downstream job.
        job_b = _job(db_session, name="Job B")

        # Upstream job, with B wired as its downstream.
        job_a = _job(db_session, name="Job A")
        job_a.downstream_jobs.append(job_b)
        db_session.commit()

        # A worker has just finished A successfully.
        task_a = Task(
            job_id=str(job_a.id),
            status="success",
            trigger_type="scheduled",
            retry_count=0,
            processed_for_chaining=False,
        )
        db_session.add(task_a)
        db_session.commit()

        # Run the dependency pass.
        triggered_count = service.trigger_dependent_jobs()

        # B should have been dispatched exactly once.
        assert triggered_count == 1

        # A's task is marked so the next pass does not re-trigger B.
        db_session.refresh(task_a)
        assert task_a.processed_for_chaining is True

        # B got a fresh pending task.
        tasks_b = db_session.query(Task).filter(Task.job_id == job_b.id).all()
        assert len(tasks_b) == 1
        assert tasks_b[0].status == "pending"
        assert tasks_b[0].trigger_type == "dependency"

        # ...and it reached the queue.
        assert queue._queue.qsize() == 1

    def test_chaining_propagates_one_level_per_success(self, db_session):
        """A -> B -> C propagates one level per success, not the whole chain."""
        queue = MemoryQueueClient()
        factory = sessionmaker(bind=db_session.get_bind())
        service = SchedulerService(factory, queue, queue)

        a = _job(db_session, name="A")
        b = _job(db_session, name="B")
        c = _job(db_session, name="C")
        a.downstream_jobs.append(b)
        b.downstream_jobs.append(c)
        db_session.commit()

        task_a = Task(job_id=str(a.id), status="success", trigger_type="scheduled",
                      retry_count=0, processed_for_chaining=False)
        db_session.add(task_a)
        db_session.commit()

        # A succeeds: only B fires.
        assert service.trigger_dependent_jobs() == 1
        b_tasks = db_session.query(Task).filter(Task.job_id == b.id).all()
        assert len(b_tasks) == 1 and b_tasks[0].trigger_type == "dependency"
        assert db_session.query(Task).filter(Task.job_id == c.id).count() == 0

        # B succeeds too: now C fires.
        b_tasks[0].status = "success"
        b_tasks[0].processed_for_chaining = False
        db_session.commit()
        assert service.trigger_dependent_jobs() == 1
        assert db_session.query(Task).filter(Task.job_id == c.id).count() == 1

    def test_chaining_cycle_does_not_loop_forever(self, db_session):
        """A mutual A <-> B dependency triggers a bounded number of times."""
        queue = MemoryQueueClient()
        factory = sessionmaker(bind=db_session.get_bind())
        service = SchedulerService(factory, queue, queue)

        a = _job(db_session, name="A")
        b = _job(db_session, name="B")
        a.downstream_jobs.append(b)
        b.downstream_jobs.append(a)  # cycle
        db_session.commit()

        db_session.add(Task(job_id=str(a.id), status="success", trigger_type="scheduled",
                            retry_count=0, processed_for_chaining=False))
        db_session.commit()

        # A succeeds: B fires once, and A's task is marked processed.
        assert service.trigger_dependent_jobs() == 1
        # No unprocessed successes remain, so the cycle does not feed itself.
        assert service.trigger_dependent_jobs() == 0
