from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.autoscaler_service import (
    AutoScaler,
    DEPTH_PER_VM,
    MAX_VMS,
    MIN_VMS,
)


@pytest.fixture
def autoscaler():
    """Build an AutoScaler with a fake SQS client. Each queue's depth is patched per test."""
    settings = MagicMock()
    settings.queue_backend = "sqs"
    settings.aws_region = "us-east-1"
    settings.sqs_endpoint_url = "http://localhost:4566"
    settings.aws_access_key_id = "x"
    settings.aws_secret_access_key = "x"
    settings.aws_session_token = None
    settings.queue_name_normal = "dass-tasks-normal"
    settings.queue_name_scheduled = "dass-tasks-scheduled"
    settings.queue_name_retry = "dass-tasks-retry"

    a = AutoScaler.__new__(AutoScaler)
    a.settings = settings
    a.enabled = True
    a.client = MagicMock()
    return a


def _patch_stats(autoscaler, *, normal: int = 0, scheduled: int = 0, retry: int = 0) -> None:
    mapping = {
        "dass-tasks-normal": normal,
        "dass-tasks-scheduled": scheduled,
        "dass-tasks-retry": retry,
    }
    autoscaler._stats = lambda name: mapping[name]


class TestAutoScalerDecide:
    def test_idle_returns_min_vms(self, autoscaler):
        _patch_stats(autoscaler)
        desired, snapshot = autoscaler.decide()
        assert desired == MIN_VMS
        assert snapshot["depth_vms"] == 0
        assert snapshot["depth"] == {"normal": 0, "scheduled": 0, "retry": 0}

    def test_depth_drives_scale_up(self, autoscaler):
        # 50 msgs / DEPTH_PER_VM=20 → ceil = 3
        _patch_stats(autoscaler, normal=50)
        desired, snapshot = autoscaler.decide()
        assert snapshot["depth_vms"] == 3
        assert desired == 3

    def test_depth_sums_across_queues(self, autoscaler):
        # 10 + 10 + 5 = 25 → ceil(25/20) = 2
        _patch_stats(autoscaler, normal=10, scheduled=10, retry=5)
        desired, snapshot = autoscaler.decide()
        assert snapshot["depth_vms"] == 2
        assert desired == 2

    def test_partial_capacity_rounds_up(self, autoscaler):
        # 1 msg → ceil(1/20) = 1 → desired=max(MIN_VMS, 1) = MIN_VMS (assuming MIN_VMS>=1)
        _patch_stats(autoscaler, normal=1)
        desired, snapshot = autoscaler.decide()
        assert snapshot["depth_vms"] == 1
        assert desired == max(MIN_VMS, 1)

    def test_clamps_to_min_vms(self, autoscaler):
        _patch_stats(autoscaler)
        desired, _ = autoscaler.decide()
        assert desired >= MIN_VMS

    def test_clamps_to_max_vms(self, autoscaler):
        # Huge backlog would exceed MAX_VMS
        _patch_stats(autoscaler, normal=10000)
        desired, _ = autoscaler.decide()
        assert desired == MAX_VMS

    def test_disabled_returns_early(self):
        settings = MagicMock()
        settings.queue_backend = "memory"
        scaler = AutoScaler(settings)
        assert scaler.enabled is False
        # apply() should be a no-op
        scaler.apply()
