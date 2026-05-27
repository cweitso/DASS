from __future__ import annotations

import concurrent.futures
import json
import logging
import sys
import threading
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


# Per spec：一個 worker VM 每條 queue 最多跑 2 個 job container
CONTAINERS_PER_VM = 2


def run_scheduler() -> None:
    """啟動 Scheduler 主迴圈：dispatch due jobs + recover orphans。"""
    settings = get_settings()
    configure_logging(level=settings.log_level)

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


def _run_dedicated_pool(queue, queue_name: str, max_workers: int, settings, retry_queue) -> None:
    """Blocking worker loop dedicated to a single queue. Runs until KeyboardInterrupt."""

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

    def _execute_task(task_msg) -> bool:
        task_id = _extract_task_id(task_msg.body)
        if not task_id:
            logger.warning("[%s] Message payload missing task_id", queue_name)
            queue.delete_message(task_msg.receipt_handle)
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
                success = service.process_task_id(str(task_id), extend_visibility=extend_visibility)
            if success:
                queue.delete_message(task_msg.receipt_handle)
                logger.info("[%s] Finished task_id=%s", queue_name, task_id)
            else:
                logger.warning("[%s] Task not claimed, message kept. task_id=%s", queue_name, task_id)
            return success
        except Exception:
            logger.exception("[%s] Error processing task_id=%s", queue_name, task_id)
            return False

    in_flight: set[concurrent.futures.Future] = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        while True:
            try:
                done = {f for f in in_flight if f.done()}
                in_flight -= done
                available = max_workers - len(in_flight)
                if available <= 0:
                    concurrent.futures.wait(in_flight, timeout=1, return_when=concurrent.futures.FIRST_COMPLETED)
                    continue
                for msg in queue.receive_tasks(max_messages=available, wait_time_seconds=2):
                    in_flight.add(executor.submit(_execute_task, msg))
            except KeyboardInterrupt:
                logger.info("[%s] Pool stopped.", queue_name)
                break
            except Exception:
                logger.exception("[%s] Pool loop error", queue_name)
                time.sleep(5)


def run_worker() -> None:
    """啟動 Worker：每條 queue 各自一個專屬 pool，三個 pool 平行執行。"""
    settings = get_settings()
    configure_logging(settings.log_level)

    normal_queue = get_normal_queue_client()
    scheduled_queue = get_scheduled_queue_client()
    retry_queue = get_retry_queue_client()

    logger.info(
        "Worker '%s' started. pools=3, containers_per_pool=%d",
        settings.worker_id,
        CONTAINERS_PER_VM,
    )

    pool_threads = [
        threading.Thread(
            target=_run_dedicated_pool,
            args=(q, name, CONTAINERS_PER_VM, settings, retry_queue),
            daemon=True,
            name=f"pool-{name}",
        )
        for q, name in [
            (normal_queue, "normal"),
            (scheduled_queue, "scheduled"),
            (retry_queue, "retry"),
        ]
    ]

    for t in pool_threads:
        t.start()

    try:
        for t in pool_threads:
            t.join()
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
        run_worker()
    elif command == "autoscaler":
        run_autoscaler()
    else:
        raise SystemExit("Unknown command: " + command)


if __name__ == "__main__":
    main()
