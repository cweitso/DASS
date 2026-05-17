from __future__ import annotations

import json

import boto3

from app.core.config import Settings
from app.queue.base import QueueMessage


class SQSQueueClient:
    """AWS SQS queue client (LocalStack for local dev).

    Conforms to the QueueClient Protocol structurally; no inheritance needed.
    """

    def __init__(self, settings: Settings, queue_name: str | None = None):
        self.settings = settings
        self._queue_name = queue_name or settings.queue_name
        self.client = boto3.client(
            "sqs",
            region_name=settings.aws_region,
            endpoint_url=settings.sqs_endpoint_url,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            aws_session_token=settings.aws_session_token,
        )
        self.queue_url = self._resolve_queue_url()

    def _resolve_queue_url(self) -> str:
        # Look up the queue; create it on first run if missing.
        try:
            response = self.client.get_queue_url(QueueName=self._queue_name)
            return response["QueueUrl"]
        except self.client.exceptions.QueueDoesNotExist:
            response = self.client.create_queue(QueueName=self._queue_name)
            return response["QueueUrl"]

    def send_task(self, task_id: str) -> None:
        self.client.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps({"task_id": task_id}),
        )

    def receive_tasks(self, max_messages: int = 1, wait_time_seconds: int = 10) -> list[QueueMessage]:
        # Long polling: SQS waits up to wait_time_seconds for a message.
        response = self.client.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=wait_time_seconds,
            VisibilityTimeout=self.settings.worker_visibility_timeout_seconds,
        )
        return [
            QueueMessage(body=message["Body"], receipt_handle=message["ReceiptHandle"])
            for message in response.get("Messages", [])
        ]

    def delete_message(self, receipt_handle: str) -> None:
        self.client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt_handle)
