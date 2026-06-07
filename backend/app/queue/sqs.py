from __future__ import annotations

import json

import boto3
from botocore.config import Config

from app.core.config import Settings
from app.queue.base import QueueMessage

# botocore 預設每個 client 只有 10 條 HTTP 連線。api-server 的 /trigger 是同步端點、
# 跑在 FastAPI 執行緒池（預設 ~40 緒），又共用同一個 lru_cache 的 client；10 條連線會
# 把高併發的 send_message 卡成序列化（壓測 trigger 階段的隱形天花板）。開大到能蓋過
# 執行緒池並留餘裕。
_MAX_POOL_CONNECTIONS = 50


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
            config=Config(max_pool_connections=_MAX_POOL_CONNECTIONS),
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
        # 短 visibility + worker 端 heartbeat 動態延長：crash 時 SQS message 很快回到可見狀態，
        # scheduler.recover_orphans 也只要等 DB lock 過期（同樣 30s 等級）就能 reclaim
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

    def change_message_visibility(self, receipt_handle: str, visibility_seconds: int) -> None:
        self.client.change_message_visibility(
            QueueUrl=self.queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=visibility_seconds,
        )
