from __future__ import annotations

import json

import pytest

from app.queue.memory import MemoryQueueClient


class TestMemoryQueueClient:
    """Tests for in-memory queue implementation."""

    def test_send_and_receive_single_task(self):
        queue = MemoryQueueClient()
        queue.send_task("task-123")

        messages = queue.receive_tasks(max_messages=1)
        assert len(messages) == 1
        assert json.loads(messages[0].body)["task_id"] == "task-123"

    def test_receive_multiple_tasks(self):
        queue = MemoryQueueClient()
        queue.send_task("task-1")
        queue.send_task("task-2")
        queue.send_task("task-3")

        messages = queue.receive_tasks(max_messages=2)
        assert len(messages) == 2
        bodies = [json.loads(m.body)["task_id"] for m in messages]
        assert "task-1" in bodies
        assert "task-2" in bodies

    def test_receive_empty_queue_returns_empty_list(self):
        queue = MemoryQueueClient()
        messages = queue.receive_tasks(max_messages=1, wait_time_seconds=0)
        assert messages == []

    def test_delete_message_is_noop(self):
        queue = MemoryQueueClient()
        queue.send_task("task-x")
        messages = queue.receive_tasks(max_messages=1)
        receipt_handle = messages[0].receipt_handle

        queue.delete_message(receipt_handle)

        # In-memory queue doesn't track deletes; receipt_handle is just ignored.
        # Test that delete doesn't raise.
        assert True

    def test_two_instances_are_isolated(self):
        normal = MemoryQueueClient()
        retry = MemoryQueueClient()

        normal.send_task("normal-task")
        retry.send_task("retry-task")

        normal_msgs = normal.receive_tasks(max_messages=5, wait_time_seconds=0)
        retry_msgs = retry.receive_tasks(max_messages=5, wait_time_seconds=0)

        assert len(normal_msgs) == 1
        assert json.loads(normal_msgs[0].body)["task_id"] == "normal-task"
        assert len(retry_msgs) == 1
        assert json.loads(retry_msgs[0].body)["task_id"] == "retry-task"

    def test_fifo_order(self):
        queue = MemoryQueueClient()
        for i in range(5):
            queue.send_task(f"task-{i}")

        messages = queue.receive_tasks(max_messages=5)
        bodies = [json.loads(m.body)["task_id"] for m in messages]
        assert bodies == ["task-0", "task-1", "task-2", "task-3", "task-4"]
