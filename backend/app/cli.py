"""Process entrypoints: `python -m app.cli [scheduler|worker|autoscaler]`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time

from sqlalchemy import create_engine, text

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

logger = logging.getLogger(__name__)

# Concurrent job containers per queue pool. Each queue (normal / scheduled / retry)
# gets its own budget, so a busy queue never starves a quiet one. A worker running
# all three pools therefore runs up to 3 x this value at once.
MAX_CONCURRENT_PER_QUEUE = 2

QUEUE_FACTORIES = {
    "normal": get_normal_queue_client,
    "scheduled": get_scheduled_queue_client,
    "retry": get_retry_queue_client,
}

# Arbitrary but fixed: every scheduler replica contends on this one advisory lock.
_LEADER_LOCK_KEY = 114514


# ── Scheduler ──────────────────────────────────────────────────────────────────


class LeaderLock:
    """Single-leader election on a PostgreSQL session-level advisory lock.

    The lock lives for as long as the connection holding it, so leadership is lost
    automatically when the process dies. Acquire it once and then only verify the
    connection: re-running pg_try_advisory_lock every cycle would stack the lock's
    reference count without ever releasing it.

    Must run on a direct PostgreSQL connection. PgBouncer's transaction pooling
    returns the server connection to the pool at commit and the lock goes with it.
    """

    def __init__(self, engine, key: int):
        self._engine = engine
        self._key = key
        self._conn = None
        self._is_leader = False

    def is_leader(self) -> bool:
        try:
            if self._conn is None:
                self._conn = self._engine.connect()
                self._is_leader = False

            if self._is_leader:
                # The lock is only as alive as its session; prove the session is up.
                self._conn.execute(text("SELECT 1"))
                return True

            self._is_leader = bool(
                self._conn.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": self._key}
                ).scalar()
            )
            return self._is_leader
        except Exception:
            logger.exception("Leader lock connection failed; standing down")
            self._reset()
            return False

    def _reset(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                logger.debug("Leader lock connection close failed", exc_info=True)
        self._conn = None
        self._is_leader = False


def run_scheduler() -> None:
    """Dispatch due cron jobs, trigger dependents, and reclaim orphaned tasks."""
    settings = get_settings()
    configure_logging(settings.log_level)

    service = SchedulerService(
        SessionLocal,
        get_scheduled_queue_client(),
        get_normal_queue_client(),
        settings.worker_visibility_timeout_seconds,
    )
    leader = LeaderLock(
        create_engine(settings.database_url, pool_size=1, max_overflow=0),
        _LEADER_LOCK_KEY,
    )

    logger.info("Scheduler started. interval=%ss", settings.scheduler_interval_seconds)
    while True:
        if leader.is_leader():
            try:
                service.sync_jobs()
                service.recover_orphans()
                service.dispatch_due_jobs()
                service.trigger_dependent_jobs()
            except Exception:
                logger.exception("Scheduler cycle failed")
        else:
            logger.debug("Standby: another scheduler holds the leader lock")
        time.sleep(settings.scheduler_interval_seconds)


# ── Autoscaler ─────────────────────────────────────────────────────────────────


def run_autoscaler() -> None:
    """Resize the Docker worker fleet from queue depth."""
    settings = get_settings()
    configure_logging(settings.log_level)

    if settings.execution_backend == "kubernetes":
        logger.info("Autoscaler disabled: KEDA owns worker scaling in Kubernetes mode.")
        return

    autoscaler = AutoScaler(settings)
    if not autoscaler.enabled:
        logger.info("Autoscaler disabled: queue_backend=%s", settings.queue_backend)
        return

    logger.info("Autoscaler started. interval=%ss", settings.autoscaler_interval_seconds)
    while True:
        try:
            autoscaler.apply()
        except Exception:
            logger.exception("Autoscaler cycle failed")
        time.sleep(settings.autoscaler_interval_seconds)


# ── Worker ─────────────────────────────────────────────────────────────────────


def _extract_task_id(body: str) -> str | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body or None
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        return payload.get("task_id")
    return None


async def _run_queue_pool(queue, queue_name: str, settings, retry_queue) -> None:
    """Consume one queue, running up to MAX_CONCURRENT_PER_QUEUE tasks at a time.

    Everything runs on one event loop: job execution is I/O bound (a container that
    makes an HTTP call, mostly), so concurrency costs coroutines rather than threads.
    """
    in_flight = 0

    async def handle(message) -> None:
        nonlocal in_flight
        in_flight += 1
        try:
            task_id = _extract_task_id(message.body)
            if not task_id:
                logger.warning("[%s] Message payload has no task_id", queue_name)
                await asyncio.to_thread(queue.delete_message, message.receipt_handle)
                return

            def extend_visibility(seconds: int) -> None:
                queue.change_message_visibility(message.receipt_handle, seconds)

            logger.info("[%s] Processing task_id=%s", queue_name, task_id)
            with SessionLocal() as db:
                service = WorkerService(
                    db=db,
                    queue_client=queue,
                    worker_id=settings.worker_id,
                    claim_seconds=settings.worker_visibility_timeout_seconds,
                    retry_queue=retry_queue,
                )
                done = await service.process_task_id_async(
                    str(task_id), extend_visibility
                )

            if done:
                await asyncio.to_thread(queue.delete_message, message.receipt_handle)
                logger.info("[%s] Finished task_id=%s", queue_name, task_id)
            else:
                # Someone else holds the task. Leave the message so it reappears
                # after its visibility timeout.
                logger.warning(
                    "[%s] Task not claimed, message kept. task_id=%s",
                    queue_name,
                    task_id,
                )
        except Exception:
            logger.exception("[%s] Failed to process message", queue_name)
        finally:
            in_flight -= 1

    while True:
        try:
            available = MAX_CONCURRENT_PER_QUEUE - in_flight
            if available <= 0:
                await asyncio.sleep(0.05)
                continue
            messages = await asyncio.to_thread(
                queue.receive_tasks, max_messages=available, wait_time_seconds=2
            )
            for message in messages:
                asyncio.create_task(handle(message))
        except asyncio.CancelledError:
            logger.info("[%s] Pool cancelled", queue_name)
            raise
        except Exception:
            logger.exception("[%s] Pool loop error", queue_name)
            await asyncio.sleep(5)


def run_worker(queue: str | None = None) -> None:
    """Run worker pools.

    queue=None    → all three pools in one process (Docker Compose mode)
    queue=<name>  → a single pool (Kubernetes mode, one Deployment per queue)
    """
    if queue is not None and queue not in QUEUE_FACTORIES:
        raise SystemExit(
            f"Unknown queue: {queue!r}. Valid values: {sorted(QUEUE_FACTORIES)}"
        )

    settings = get_settings()
    configure_logging(settings.log_level)

    retry_queue = get_retry_queue_client()
    names = [queue] if queue is not None else list(QUEUE_FACTORIES)
    pools = [(name, QUEUE_FACTORIES[name]()) for name in names]

    logger.info(
        "Worker '%s' started. queues=%s concurrency=%d per queue (%d total)",
        settings.worker_id,
        names,
        MAX_CONCURRENT_PER_QUEUE,
        MAX_CONCURRENT_PER_QUEUE * len(pools),
    )

    async def run_all() -> None:
        await asyncio.gather(
            *[
                _run_queue_pool(client, name, settings, retry_queue)
                for name, client in pools
            ]
        )

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        logger.info("Worker stopped by user.")


# ── Entrypoint ─────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("scheduler", help="run the scheduler loop")
    subparsers.add_parser("autoscaler", help="run the Docker worker autoscaler")
    worker_parser = subparsers.add_parser("worker", help="run worker queue pools")
    worker_parser.add_argument(
        "--queue",
        choices=sorted(QUEUE_FACTORIES),
        default=None,
        help="consume only this queue (default: all three)",
    )

    args = parser.parse_args()
    if args.command == "scheduler":
        run_scheduler()
    elif args.command == "autoscaler":
        run_autoscaler()
    else:
        run_worker(queue=args.queue)


if __name__ == "__main__":
    main()
