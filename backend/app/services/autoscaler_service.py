from __future__ import annotations

import logging
import math

import boto3

from app.core.config import Settings
from app.services.vm_service import vm_service

logger = logging.getLogger(__name__)


# Worker VM 容量
CONTAINERS_PER_VM = 2
MIN_VMS = 1
MAX_VMS = 10

# 每 N 條訊息(visible 積壓 + in-flight 處理中)對應 1 個 VM 容量;單一 depth 訊號同時驅動 scale-up/down
DEPTH_PER_VM = 20


class AutoScaler:
    """根據 queue 總工作量 (visible + in-flight) 決定 worker VM 數,套用到 VMService。

    Note:ApproximateAgeOfOldestMessage 是 CloudWatch metric 不是 queue attribute,
    GetQueueAttributes 會 reject。SLA-based 擴容若日後需要,改接 CloudWatch
    GetMetricStatistics 補回來。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = settings.queue_backend == "sqs"
        if not self.enabled:
            logger.info("AutoScaler disabled: queue_backend=%s", settings.queue_backend)
            self.client = None
            return

        self.client = boto3.client(
            "sqs",
            region_name=settings.aws_region,
            endpoint_url=settings.sqs_endpoint_url,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            aws_session_token=settings.aws_session_token,
        )

    def _stats(self, queue_name: str) -> int:
        """回 queue 總工作量 = visible(積壓) + not-visible(處理中)；錯誤時回 0 不讓 autoscaler 翻車。

        只算 visible 會在「訊息都被 worker 撈走變隱形、但還在跑」時把 depth 誤判成 0，
        提前 scale-down 砍掉忙碌中的 worker（被砍的 in-flight task 雖能靠 recover_orphans
        回收，但會 churn）。把 in-flight 一起算進來後，desired 反映真實負載，scale-down
        只在真的沒事做時才發生。
        """
        try:
            url = self.client.get_queue_url(QueueName=queue_name)["QueueUrl"]
            attrs = self.client.get_queue_attributes(
                QueueUrl=url,
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            )["Attributes"]
            visible = int(attrs.get("ApproximateNumberOfMessages", 0))
            in_flight = int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0))
            return visible + in_flight
        except Exception:
            logger.exception("Failed to read stats for queue=%s", queue_name)
            return 0

    def decide(self) -> tuple[int, dict]:
        queues = [
            (self.settings.queue_name_normal, "normal"),
            (self.settings.queue_name_scheduled, "scheduled"),
            (self.settings.queue_name_retry, "retry"),
        ]
        depths = {label: self._stats(name) for name, label in queues}

        total_depth = sum(depths.values())
        depth_vms = math.ceil(total_depth / DEPTH_PER_VM) if total_depth else 0

        desired = max(MIN_VMS, min(MAX_VMS, depth_vms))

        snapshot = {
            "depth": depths,
            "depth_vms": depth_vms,
            "desired": desired,
        }
        return desired, snapshot

    def apply(self) -> None:
        if not self.enabled:
            return

        desired, snapshot = self.decide()
        current = len(vm_service.get_active_vms())
        diff = desired - current

        logger.info(
            "autoscale: current=%s desired=%s diff=%s snapshot=%s",
            current, desired, diff, snapshot,
        )

        if diff > 0:
            vm_service.create_vms(diff)
        elif diff < 0:
            vm_service.terminate_vms(-diff)
