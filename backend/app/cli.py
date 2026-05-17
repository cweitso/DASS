from __future__ import annotations

import json
import logging
import sys
import time

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.queue.factory import get_queue_client
from app.services.scheduler_service import SchedulerService
from app.services.worker_service import WorkerService

logger = logging.getLogger(__name__)


def run_scheduler() -> None:
    """啟動 Scheduler 主迴圈。

    # TODO:
    #   1. 取得 settings，設定 logging
    #   2. 取得 queue_client
    #   3. 無限迴圈：
    #      a. 開一個 DB session（with SessionLocal() as db）
    #      b. 建立 SchedulerService(db, queue, settings.worker_visibility_timeout_seconds)
    #      c. 執行 recover_orphans()
    #      d. 執行 dispatch_due_jobs()
    #      e. sleep(settings.scheduler_interval_seconds)
    """
    raise NotImplementedError


import concurrent.futures

def run_worker() -> None:
    """啟動 Worker 主迴圈：支援 normal queue 優先、多 Queue、平行處理 containers。"""
    settings = get_settings()
    configure_logging(settings.log_level)

    # 目前如果 factory 還沒支援多 queue，就先共用同一個 queue。
    normal_queue = get_queue_client()
    retry_queue = get_queue_client()

    max_workers = getattr(settings, "worker_concurrency", 5)

    logger.info(
        "Worker '%s' started. concurrency=%s",
        settings.worker_id,
        max_workers,
    )

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

    def _execute_task(task_msg, source_queue) -> bool:
        task_id = _extract_task_id(task_msg.body)

        if not task_id:
            logger.warning("Message payload missing task_id")
            source_queue.delete_message(task_msg.receipt_handle)
            return False

        try:
            logger.info("Processing task_id=%s", task_id)

            with SessionLocal() as db:
                service = WorkerService(
                    db=db,
                    queue_client=source_queue,
                    worker_id=settings.worker_id,
                    claim_seconds=settings.worker_visibility_timeout_seconds,
                )
                success = service.process_task_id(str(task_id))

            if success:
                source_queue.delete_message(task_msg.receipt_handle)
                logger.info("Finished and deleted task_id=%s", task_id)
            else:
                logger.warning(
                    "Task failed or was not claimed. Message kept for retry. task_id=%s",
                    task_id,
                )

            return success

        except Exception:
            logger.exception("Error processing task_id=%s", task_id)
            return False

    in_flight: set[concurrent.futures.Future] = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        while True:
            try:
                # 清掉已完成的 futures，避免 in_flight 越長越大
                done = {future for future in in_flight if future.done()}
                in_flight -= done

                available_slots = max_workers - len(in_flight)

                if available_slots <= 0:
                    concurrent.futures.wait(
                        in_flight,
                        timeout=1,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    continue

                # normal queue 優先
                messages = normal_queue.receive_tasks(
                    max_messages=available_slots,
                    wait_time_seconds=5,
                )
                source_queue = normal_queue

                # normal 沒東西才拿 retry
                if not messages:
                    messages = retry_queue.receive_tasks(
                        max_messages=available_slots,
                        wait_time_seconds=5,
                    )
                    source_queue = retry_queue

                if not messages:
                    continue

                for msg in messages:
                    future = executor.submit(_execute_task, msg, source_queue)
                    in_flight.add(future)

            except KeyboardInterrupt:
                logger.info("Worker stopped by user.")
                break

            except Exception:
                logger.exception("Worker loop error")
                time.sleep(5)

def main() -> None:
    """CLI 入口：根據 sys.argv[1] 分派到 scheduler 或 worker。"""
    if len(sys.argv) < 2:
        print("Usage: python -m app.cli [scheduler|worker]")
        sys.exit(1)
        
    command = sys.argv[1]
    
    if command == "scheduler":
        run_scheduler()
    elif command == "worker":
        run_worker()
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
