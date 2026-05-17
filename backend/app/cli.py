from __future__ import annotations

import json
import logging
import sys
import time

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.queue.factory import get_queue_client, get_retry_queue_client
from app.services.scheduler_service import SchedulerService
from app.services.worker_service import WorkerService

logger = logging.getLogger(__name__)


def run_scheduler() -> None:
    """啟動 Scheduler 主迴圈。
    #   1. 取得 settings，設定 logging
    #   2. 取得 queue_client
    #   3. 無限迴圈：
    #      a. 開一個 DB session（with SessionLocal() as db）
    #      b. 建立 SchedulerService(db, queue, settings.worker_visibility_timeout_seconds)
    #      c. 執行 recover_orphans()
    #      d. 執行 dispatch_due_jobs()
    #      e. sleep(settings.scheduler_interval_seconds)
    # TODO:
    #   完成這次 MVP 後去測試 scheduler() 是否會變成效能瓶頸
    """
    settings = get_settings()
    configure_logging(level=settings.log_level)

    queue_client = get_queue_client()

    while True:
        try:
            with SessionLocal() as db:
                service = SchedulerService(db, queue_client, settings.worker_visibility_timeout_seconds)
                service.recover_orphans()
                service.dispatch_due_jobs()
        except Exception as e:
            logger.error(f"Scheduler cycle failed: {e}")
        finally:
            time.sleep(settings.scheduler_interval_seconds)


def run_worker() -> None:
    """啟動 Worker 主迴圈。
    #   1. 取得 settings，設定 logging
    #   2. 取得 queue_client
    #   3. 無限迴圈：
    #      a. queue.receive_tasks(max_messages=1, wait_time_seconds=10)
    #      b. 若無訊息 → continue
    #      c. 對每個 message：
    #         - 解析 payload = json.loads(message.body)
    #         - 取出 task_id
    #         - 開 DB session，建立 WorkerService
    #         - 呼叫 service.process_task_id(task_id)
    #         - 成功後 queue.delete_message(message.receipt_handle)
    #         - 捕獲 Exception 並 log
    """
    settings = get_settings()
    configure_logging(level=settings.log_level)

    queue_client = get_queue_client()
    retry_queue_client = get_retry_queue_client()

    while True:
        messages = queue_client.receive_tasks(max_messages=1, wait_time_seconds=10)
        if not messages:
            continue

        for message in messages:
            try:
                payload = json.loads(message.body)
                task_id = payload.get("task_id")
                with SessionLocal() as db:
                    service = WorkerService(
                        db, queue_client,
                        worker_id=settings.worker_id,
                        retry_queue=retry_queue_client,
                    )
                    service.process_task_id(task_id)
                
                queue_client.delete_message(message.receipt_handle)
            except Exception as e:
                logger.error(f"Worker failed to process message: {e}")


def main() -> None:
    """CLI 入口：根據 sys.argv[1] 分派到 scheduler 或 worker。
    #   - 'scheduler' → run_scheduler()
    #   - 'worker'    → run_worker()
    #   - 其他        → raise SystemExit
    """
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python -m app.cli scheduler|worker")

    command = sys.argv[1]
    if command == "scheduler":
        run_scheduler()
    elif command == "worker":
        run_worker()
    else:
        raise SystemExit("Unknown command: " + command)


if __name__ == "__main__":
    main()
