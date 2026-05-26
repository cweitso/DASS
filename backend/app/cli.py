from __future__ import annotations

import json
import logging
import sys
import time

from uuid import uuid4

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


# === Worker VM 容量設定 ====================================================
# Per spec：一個 worker VM 寫死最多跑 2 個 job container
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


import concurrent.futures

def run_worker() -> None:
    """啟動 Worker 主迴圈：支援 normal > scheduled > retry queue 優先、平行處理 containers。"""
    settings = get_settings()
    configure_logging(settings.log_level)

    normal_queue = get_normal_queue_client()
    scheduled_queue = get_scheduled_queue_client()
    retry_queue = get_retry_queue_client()

    # ─── Known limitation (will be fixed in S5) ─────────────────────────
    # S4 過渡期接受此行為,S5 改成「每 queue 專屬 worker pool」後此問題自然消失。
    # ──────────────────────────────────────────────────────────────────────────
    # Priority chain — 越前面越急；normal long-poll 2s，其他 1s 避免空等過久。
    queue_chain = [
        (normal_queue, "normal", 2),
        (scheduled_queue, "scheduled", 1),
        (retry_queue, "retry", 1),
    ]
    
    # max_workers = getattr(settings, "worker_concurrency", 5)  # 原預設值
    max_workers = CONTAINERS_PER_VM  # 目前寫死，一個 worker VM 兩個 container

    logger.info(
        "Worker '%s' started. containers_per_vm=%s",
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

        def extend_visibility(seconds: int) -> None:
            source_queue.change_message_visibility(task_msg.receipt_handle, seconds)

        try:
            logger.info("Processing task_id=%s", task_id)

            with SessionLocal() as db:
                # S4 修正：顯式傳 retry_queue=retry_queue，否則 WorkerService 預設用 source_queue
                service = WorkerService(
                    db=db,
                    queue_client=source_queue,
                    worker_id=settings.worker_id,
                    claim_seconds=settings.worker_visibility_timeout_seconds,
                    retry_queue=retry_queue,
                )
                success = service.process_task_id(
                    str(task_id),
                    extend_visibility=extend_visibility,
                )

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
    cycle_count = 0  # 輪轉指針，三個隊列輪流當最高優先級

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

                # === S4: 三條 queue 輪轉檢查，防飢餓 ==============================
                # 改進：每個 cycle 輪轉檢查順序，三個隊列都輪流當最高優先級
                # - Cycle 0: normal > scheduled > retry
                # - Cycle 1: scheduled > retry > normal
                # - Cycle 2: retry > normal > scheduled
                # - Cycle 3: 回到 normal...
                # 這樣每個隊列都有機會最高優先被檢查，不會有隊列被永遠餓死

                messages = []
                source_queue = None
                
                # 輪轉起點：每個 cycle 換一個隊列當最高優先級
                rotated_chain = queue_chain[cycle_count % 3:] + queue_chain[:cycle_count % 3]
                
                for q, _name, wait in rotated_chain:
                    messages = q.receive_tasks(
                        max_messages=available_slots,
                        wait_time_seconds=wait,
                    )
                    if messages:
                        source_queue = q
                        break
                
                cycle_count += 1

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
