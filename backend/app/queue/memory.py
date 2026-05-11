from __future__ import annotations

import json
import queue
import uuid

from app.queue.base import QueueMessage


class MemoryQueueClient:
    """In-memory queue for tests (no Docker / LocalStack required).

    Conforms to the QueueClient Protocol structurally; no inheritance needed.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()

    def send_task(self, task_id: str) -> None:
        body = json.dumps({"task_id": task_id})
        receipt_handle = str(uuid.uuid4())
        self._queue.put((body, receipt_handle))

    def receive_tasks(self, max_messages: int = 1, wait_time_seconds: int = 10) -> list[QueueMessage]:
        messages: list[QueueMessage] = []
        for _ in range(max_messages):
            try:
                body, receipt_handle = self._queue.get(timeout=wait_time_seconds)
            except queue.Empty:
                break
            messages.append(QueueMessage(body=body, receipt_handle=receipt_handle))
        return messages

    def delete_message(self, receipt_handle: str) -> None:
        # No-op: the in-memory queue removes items on get().
        return None
