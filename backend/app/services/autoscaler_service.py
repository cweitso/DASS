from __future__ import annotations

import logging
import math

import boto3

from app.core.config import Settings
from app.services.vm_service import vm_service

logger = logging.getLogger(__name__)

# Worker fleet sizing.
MIN_VMS = 1
MAX_VMS = 10
# Backlog (visible + in-flight) that one worker is expected to absorb. The same
# signal drives both scale-up and scale-down.
DEPTH_PER_VM = 20


class AutoScaler:
    """Sizes the Docker worker fleet from total queue depth.

    Kubernetes mode does not use this — KEDA owns replica counts there.
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

    def _depth(self, queue_name: str) -> int:
        """Outstanding work on a queue: waiting messages plus in-flight ones.

        Counting only visible messages reports zero while workers are busy, which
        scales the fleet down mid-run. Including in-flight messages makes the number
        track real load, so scale-down only happens when there is nothing left to do.

        Returns 0 on error rather than letting one bad read resize the fleet.
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
            return int(attrs.get("ApproximateNumberOfMessages", 0)) + int(
                attrs.get("ApproximateNumberOfMessagesNotVisible", 0)
            )
        except Exception:
            logger.exception("Failed to read depth for queue=%s", queue_name)
            return 0

    def decide(self) -> tuple[int, dict]:
        depths = {
            label: self._depth(name)
            for name, label in (
                (self.settings.queue_name_normal, "normal"),
                (self.settings.queue_name_scheduled, "scheduled"),
                (self.settings.queue_name_retry, "retry"),
            )
        }
        total = sum(depths.values())
        needed = math.ceil(total / DEPTH_PER_VM) if total else 0
        desired = max(MIN_VMS, min(MAX_VMS, needed))
        return desired, {"depth": depths, "depth_vms": needed, "desired": desired}

    def apply(self) -> None:
        if not self.enabled:
            return

        desired, snapshot = self.decide()
        current = len(vm_service.get_active_vms())
        logger.info(
            "autoscale: current=%s desired=%s snapshot=%s", current, desired, snapshot
        )

        if desired > current:
            vm_service.create_vms(desired - current)
        elif desired < current:
            vm_service.terminate_vms(current - desired)
