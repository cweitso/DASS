from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.queue.base import QueueClient
from app.queue.memory import MemoryQueueClient
from app.queue.sqs import SQSQueueClient


@lru_cache
def get_queue_client() -> QueueClient:
    """Return a QueueClient based on settings.queue_backend."""
    settings = get_settings()
    if settings.queue_backend == "memory":
        return MemoryQueueClient()
    return SQSQueueClient(settings)
