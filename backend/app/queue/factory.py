from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.queue.base import QueueClient
from app.queue.memory import MemoryQueueClient
from app.queue.sqs import SQSQueueClient


@lru_cache
def get_normal_queue_client() -> QueueClient:
    settings = get_settings()
    if settings.queue_backend == "memory":
        return MemoryQueueClient()
    return SQSQueueClient(settings, queue_name=settings.queue_name_normal)


@lru_cache
def get_retry_queue_client() -> QueueClient:
    settings = get_settings()
    if settings.queue_backend == "memory":
        return MemoryQueueClient()
    return SQSQueueClient(settings, queue_name=settings.queue_name_retry)


# backward-compat alias — callers that import get_queue_client get the normal queue
get_queue_client = get_normal_queue_client
