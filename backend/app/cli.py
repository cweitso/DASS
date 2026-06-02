from __future__ import annotations

import asyncio
import json
import logging
import sys
import time

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.queue.factory import (
    get_normal_queue_client,
    get_retry_queue_client,
    get_scheduled_queue_client,
)
from app.services.autoscaler_service import AutoScaler
from app.services.scheduler_service import SchedulerService
from app.services.worker_service import WorkerService
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)


# Per spec：一個 worker VM 每條 queue 最多跑幾個 job container
CONTAINERS_PER_VM = 10


def run_scheduler() -> None:
    """啟動 Scheduler 主迴圈：dispatch due jobs + recover orphans。"""
    settings = get_settings()
    configure_logging(level=settings.log_level)
    queue_client = get_scheduled_queue_client()
    lock_engine = create_engine(settings.database_url, pool_size=1, max_overflow=0)
    LOCK_KEY = 114514

    with lock_engine.connect() as lock_conn:
        service = SchedulerService(
            SessionLocal, queue_client, settings.worker_visibility_timeout_seconds
        )
        while True:
            is_leader = lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}
            ).scalar()
            if is_leader:
                logger.info("[LEADER] Running scheduler cycle")
                try:
                    service.sync_jobs()
                    service.recover_orphans()
                    service.dispatch_due_jobs()
                except Exception as e:
                    logger.error(f"[LEADER]Scheduler cycle failed: {e}")
            else:
                logger.debug("[STANDBY] Waiting for leader lock...")
            time.sleep(settings.scheduler_interval_seconds)

    normal_queue = get_normal_queue_client()
    scheduled_queue = get_scheduled_queue_client()

    while True:
        try:
            with SessionLocal() as db:
                service = SchedulerService(
                    db,
                    normal_queue_client=normal_queue,
                    scheduled_queue_client=scheduled_queue,
                    worker_visibility_timeout_seconds=settings.worker_visibility_timeout_seconds,
                )
                service.recover_orphans()
                service.dispatch_due_jobs()
        except Exception as e:
            logger.error(f"Scheduler cycle failed: {e}")

        time.sleep(settings.scheduler_interval_seconds)


def run_autoscaler() -> None:
    """啟動 AutoScaler 主迴圈，獨立 process。"""
    settings = get_settings()
    configure_logging(level=settings.log_level)

    if settings.execution_backend == "kubernetes":
        logger.info(
            "Autoscaler disabled: worker scaling is delegated to KEDA (execution_backend=kubernetes)."
        )
        return

    autoscaler = AutoScaler(settings)
    if not autoscaler.enabled:
        logger.info(
            "Autoscaler disabled (queue_backend=%s). Exiting.",
            settings.queue_backend,
        )
        return

    interval = settings.autoscaler_interval_seconds
    logger.info("Autoscaler started. interval=%ss", interval)

    while True:
        try:
            autoscaler.apply()
        except Exception:
            logger.exception("Autoscaler cycle failed")
        time.sleep(interval)


def _extract_task_id(body: str) -> str | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = body
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        return payload.get("task_id")
    return None


async def _run_dedicated_pool_async(queue, queue_name: str, max_concurrent: int, settings, retry_queue) -> None:
    """Async worker loop: one event loop handles max_concurrent jobs concurrently.

    For I/O-bound jobs (e.g. HTTP calls), a single thread can keep max_concurrent
    containers running at once — none of them block each other while waiting for
    network responses. Previously, each in-flight job blocked one OS thread.
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    active_count = 0

    async def _execute_task_async(task_msg) -> bool:
        nonlocal active_count
        async with semaphore:
            active_count += 1
            task_id = _extract_task_id(task_msg.body)
            if not task_id:
                logger.warning("[%s] Message payload missing task_id", queue_name)
                await asyncio.to_thread(queue.delete_message, task_msg.receipt_handle)
                active_count -= 1
                return False

            def extend_visibility(seconds: int) -> None:
                queue.change_message_visibility(task_msg.receipt_handle, seconds)

            try:
                logger.info("[%s] Processing task_id=%s", queue_name, task_id)
                with SessionLocal() as db:
                    service = WorkerService(
                        db=db,
                        queue_client=queue,
                        worker_id=settings.worker_id,
                        claim_seconds=settings.worker_visibility_timeout_seconds,
                        retry_queue=retry_queue,
                    )
                    success = await service.process_task_id_async(str(task_id), extend_visibility)
                if success:
                    await asyncio.to_thread(queue.delete_message, task_msg.receipt_handle)
                    logger.info("[%s] Finished task_id=%s", queue_name, task_id)
                else:
                    logger.warning("[%s] Task not claimed, message kept. task_id=%s", queue_name, task_id)
                return success
            except Exception:
                logger.exception("[%s] Error processing task_id=%s", queue_name, task_id)
                return False
            finally:
                active_count -= 1

    while True:
        try:
            available = max_concurrent - active_count
            if available <= 0:
                await asyncio.sleep(0.05)
                continue
            msgs = await asyncio.to_thread(
                queue.receive_tasks, max_messages=available, wait_time_seconds=2
            )
            for msg in msgs:
                asyncio.create_task(_execute_task_async(msg))
        except asyncio.CancelledError:
            logger.info("[%s] Pool cancelled.", queue_name)
            break
        except Exception:
            logger.exception("[%s] Pool loop error", queue_name)
            await asyncio.sleep(5)


_QUEUE_FACTORIES = {
    "normal": get_normal_queue_client,
    "scheduled": get_scheduled_queue_client,
    "retry": get_retry_queue_client,
}


def run_worker(queue: str | None = None) -> None:
    """啟動 Worker。

    queue=None  → 啟動全部三條 queue 的 pool（Docker Compose 模式）
    queue=<name> → 只啟動指定 queue 的 pool（K8s 每 pool 一個 Deployment 模式）
    """
    if queue is not None and queue not in _QUEUE_FACTORIES:
        raise SystemExit(f"Unknown queue: {queue!r}. Valid values: {list(_QUEUE_FACTORIES)}")

    settings = get_settings()
    configure_logging(settings.log_level)

    retry_queue = get_retry_queue_client()

    queues_to_run = (
        [(queue, _QUEUE_FACTORIES[queue]())]
        if queue is not None
        else [(name, factory()) for name, factory in _QUEUE_FACTORIES.items()]
    )

    logger.info(
        "Worker '%s' started. pools=%d, max_concurrent_per_pool=%d, queues=%s",
        settings.worker_id,
        len(queues_to_run),
        CONTAINERS_PER_VM,
        [name for name, _ in queues_to_run],
    )

    async def _run_all_pools() -> None:
        await asyncio.gather(*[
            _run_dedicated_pool_async(q, name, CONTAINERS_PER_VM, settings, retry_queue)
            for name, q in queues_to_run
        ])

    try:
        asyncio.run(_run_all_pools())
    except KeyboardInterrupt:
        logger.info("Worker stopped by user.")


def main() -> None:
    """CLI 入口：根據 sys.argv[1] 分派到 scheduler / worker / autoscaler。"""
    if len(sys.argv) < 2:
        print("Usage: python -m app.cli [scheduler|worker|autoscaler]")
        sys.exit(1)

    command = sys.argv[1]

    if command == "scheduler":
        run_scheduler()
    elif command == "worker":
        queue_arg = None
        if "--queue" in sys.argv:
            idx = sys.argv.index("--queue")
            if idx + 1 < len(sys.argv):
                queue_arg = sys.argv[idx + 1]
        run_worker(queue=queue_arg)
    elif command == "autoscaler":
        run_autoscaler()
    else:
        raise SystemExit("Unknown command: " + command)


if __name__ == "__main__":
    main()
