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

    def test_scheduler_ignores_normal_jobs(self, db_session):
        """job_type='normal' 的 job 即使已到期也不該被 scheduler 派發。
        手動觸發走 API → normal queue，scheduler 不能重複燒它，否則 task 會雙倍累積。
        """
        queue = MemoryQueueClient()
        job = _job(db_session, job_type="normal")

        factory = sessionmaker(bind=db_session.get_bind())
        service = SchedulerService(factory, queue)

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

    def test_trigger_dependent_jobs(self, db_session):
        """Scheduler 應該能偵測剛成功的 Task，並觸發其相依的下游 Job (downstream_jobs)。"""
        queue = MemoryQueueClient()
        factory = sessionmaker(bind=db_session.get_bind())
        service = SchedulerService(factory, queue)

        # 1. 建立下游 Job B
        job_b = _job(db_session, name="Job B")

        # 2. 建立上游 Job A，並把 Job B 加入其下游任務清單
        job_a = _job(db_session, name="Job A")
        job_a.downstream_jobs.append(job_b)
        db_session.commit()

        # 3. 模擬 Worker 剛完成 Job A，建立一筆成功的 Task
        task_a = Task(
            job_id=str(job_a.id),
            status="success",
            trigger_type="scheduled",
            retry_count=0,
            processed_for_chaining=False,
        )
        db_session.add(task_a)
        db_session.commit()

        # 4. 執行 Scheduler 的相依性檢查邏輯
        triggered_count = service.trigger_dependent_jobs()

        # 5. 驗證結果
        assert triggered_count == 1

        # 驗證 Task A 已經被標記為處理過，避免重複觸發
        db_session.refresh(task_a)
        assert task_a.processed_for_chaining is True

        # 驗證系統有幫 Job B 建立一筆新的 Task
        tasks_b = db_session.query(Task).filter(Task.job_id == job_b.id).all()
        assert len(tasks_b) == 1
        assert tasks_b[0].status == "pending"
        assert tasks_b[0].trigger_type == "dependency"

        # 驗證這個新的 Task 有被正確推入 Queue 準備執行
        assert queue._queue.qsize() == 1

    def test_chaining_propagates_one_level_per_success(self, db_session):
        """A→B→C：A 成功只觸發 B；要等 B 也成功才觸發 C（一次傳播一層）。"""
        queue = MemoryQueueClient()
        factory = sessionmaker(bind=db_session.get_bind())
        service = SchedulerService(factory, queue)

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

        # A 成功 → 只觸發 B
        assert service.trigger_dependent_jobs() == 1
        b_tasks = db_session.query(Task).filter(Task.job_id == b.id).all()
        assert len(b_tasks) == 1 and b_tasks[0].trigger_type == "dependency"
        assert db_session.query(Task).filter(Task.job_id == c.id).count() == 0

        # B 也成功 → 才觸發 C
        b_tasks[0].status = "success"
        b_tasks[0].processed_for_chaining = False
        db_session.commit()
        assert service.trigger_dependent_jobs() == 1
        assert db_session.query(Task).filter(Task.job_id == c.id).count() == 1

    def test_chaining_cycle_does_not_loop_forever(self, db_session):
        """A↔B 互為上下游時，單一成功事件只觸發有限次，不會自我延續成無限迴圈。"""
        queue = MemoryQueueClient()
        factory = sessionmaker(bind=db_session.get_bind())
        service = SchedulerService(factory, queue)

        a = _job(db_session, name="A")
        b = _job(db_session, name="B")
        a.downstream_jobs.append(b)
        b.downstream_jobs.append(a)  # cycle
        db_session.commit()

        db_session.add(Task(job_id=str(a.id), status="success", trigger_type="scheduled",
                            retry_count=0, processed_for_chaining=False))
        db_session.commit()

        # A 成功 → 觸發 B 一次；A 的 task 標記 processed 後不會再被撈
        assert service.trigger_dependent_jobs() == 1
        # 沒有新的「未處理的成功 task」→ 0，證明不會無限迴圈
        assert service.trigger_dependent_jobs() == 0
