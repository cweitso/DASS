from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class QueueMessage:
    body: str
    receipt_handle: str


class QueueClient(Protocol):
    def send_task(self, task_id: str) -> None: ...

    def receive_tasks(self, max_messages: int = 1, wait_time_seconds: int = 10) -> list[QueueMessage]: ...

    def delete_message(self, receipt_handle: str) -> None: ...

    def change_message_visibility(self, receipt_handle: str, visibility_seconds: int) -> None: ...

