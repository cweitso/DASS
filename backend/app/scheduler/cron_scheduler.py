from __future__ import annotations

from datetime import datetime, UTC
import logging

from sqlalchemy.orm import Session

from app.models.task import Task
from app.repositories.job_repository import JobRepository
from app.repositories.task_repository import TaskRepository
from app.utils.cron import next_cron_time
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

import heapq


class CronScheduler:
    def __init__(
        self, session_maker, queue_client, worker_visibility_timeout_seconds: int = 300
    ):
        self.session_maker = session_maker
        self.job = None
        self.task = None
        self._heap = []
        self._job_cache = {}
        self.last_sync_at = None
        self.queue = queue_client
        self.worker_visibility_timeout_seconds = worker_visibility_timeout_seconds

    def sync_jobs(self):
        """同步資料庫中異動的 Job 到排程堆疊中 (Tombstone 模式)。"""

        with self.session_maker() as db:
            jobs_repo = JobRepository(db)
            jobs = jobs_repo.list_updated_since(self.last_sync_at)
            db.expunge_all()
        for job in jobs:
            if job.enabled:
                if job.next_fire_at and job.next_fire_at.tzinfo is None:
                    job.next_fire_at = job.next_fire_at.replace(tzinfo=UTC)

                cached_job = self._job_cache.get(str(job.id))
                # 若 cache 中沒有這個 job，或新的下次執行時間跟 cache 裡的不同，才 push 進 Heap
                if job.next_fire_at is not None:
                    if (
                        not cached_job
                        or cached_job.next_fire_at is None
                        or abs(
                            cached_job.next_fire_at.timestamp()
                            - job.next_fire_at.timestamp()
                        )
                        > 0.001
                    ):
                        heapq.heappush(
                            self._heap, (job.next_fire_at.timestamp(), str(job.id))
                        )

                self._job_cache[str(job.id)] = job
            else:
                self._job_cache.pop(str(job.id), None)

        self.last_sync_at = utcnow()

    def recover_orphans(self) -> int:
        """回收所有 locked_until 過期的 running Task：把 status 改回 pending。

        不需要重送 message：worker 端 heartbeat 同步延長 SQS visibility 與 DB locked_until，
        兩者會一起過期。SQS visibility 過期後 message 自動 visible，會被下個 worker 撈到。
        此處只負責讓 atomic claim（WHERE status='pending'）能再次成功。

        若 heartbeat 異常導致 DB lock 過期但 SQS visibility 仍活，避免雙路徑造成
        duplicate execution——所以這裡不主動 resend。
        """
        now = utcnow()
        with self.session_maker() as db:
            task_repo = TaskRepository(db)
            tasks = task_repo.list_expired_running(now)
            for task in tasks:
                task_repo.mark_running_expired_pending(task)
                # 這裡依循 main 的邏輯，拿掉 self.queue.send_task()

        return len(tasks)

    def dispatch_due_jobs(self) -> int:
        now = utcnow()
        counter = 0
        start_time = now.timestamp()

        logger.info(
            f"[Scheduler] dispatch_due_jobs started. Heap size: {len(self._heap)}"
        )

        while self._heap:
            next_time, job_id = self._heap[0]
            if next_time > now.timestamp():
                logger.info(
                    f"[Scheduler] Break! next_time={next_time} > now={now.timestamp()} Diff={next_time - now.timestamp()}"
                )
                break
            next_time, job_id = heapq.heappop(self._heap)

            job = self._job_cache.get(job_id)
            if job is None:
                logger.info(
                    f"[Scheduler] Job {job_id} not found in cache (might be deleted/disabled)."
                )
                continue

            # 處理 Float 精度誤差，容忍 0.001 秒差異
            if abs(job.next_fire_at.timestamp() - next_time) > 0.001:
                logger.info(
                    f"[Scheduler] Job {job_id} next_fire_at mismatch. Cache: {job.next_fire_at.timestamp()}, Heap: {next_time}. Skipping old heap node."
                )
                continue

            with self.session_maker() as db:
                self.job = JobRepository(db)
                self.task = TaskRepository(db)
                job = db.merge(job)

                # 執行發射
                success = self._dispatch_job(job, now)

                if job.next_fire_at and job.next_fire_at.tzinfo is None:
                    job.next_fire_at = job.next_fire_at.replace(tzinfo=UTC)

                # 【不管成功或失敗】，它的 next_fire_at 都已經算好了，必須存回快取並 Push 回 Heap 排隊
                self._job_cache[str(job_id)] = job
                if job.next_fire_at is not None:
                    heapq.heappush(
                        self._heap, (job.next_fire_at.timestamp(), str(job_id))
                    )

                # 只有真正發射成功才算 counter
                if success:
                    counter += 1

        elapsed = utcnow().timestamp() - start_time
        if counter > 0:
            logger.info(
                f"[Scheduler] Dispatched {counter} jobs in {elapsed:.3f} seconds. Throughput: {counter / elapsed:.2f} jobs/s"
            )

        return counter

    def _dispatch_job(self, job, now: datetime) -> bool:
        """派發單一 Job：建立 Task、送入 Queue、更新 next_fire_at"""
        job.next_fire_at = next_cron_time(job.cron_expression, now)
        self.job.update(job)
        if (
            job.concurrency_policy == "forbid"
            and self.task.count_running_for_job(job.id) > 0
        ):
            return False
        task = Task(
            job_id=str(job.id),
            status="pending",
            trigger_type="scheduled",
            retry_count=0,
        )
        self.task.create(task)
        self.queue.send_task(str(task.id))
        return True
