"""
Integration test fixtures — 使用真實 PostgreSQL + LocalStack SQS。

環境變數由 integration-ci.yml 的 services 注入；
本地開發時可用 docker-compose + .env.test 提供。
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.job import Job
from app.models.task import Task


# ── Marker 註冊 ──────────────────────────────────────────
def pytest_configure(config):
    config.addinivalue_line("markers", "integration: 整合測試 (需要 Docker services)")


# ── DB Fixtures ──────────────────────────────────────────

@pytest.fixture(scope="session")
def main_engine():
    """主庫 engine（整個 test session 共用）。"""
    url = os.environ.get(
        "DASS_DATABASE_URL",
        "postgresql+psycopg://dass:dass@localhost:5432/dass_test",
    )
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def scheduler_engine():
    """Scheduler Local DB engine。"""
    url = os.environ.get(
        "DASS_SCHEDULER_DB_URL",
        "postgresql+psycopg://dass:dass@localhost:5433/dass_scheduler",
    )
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def main_db(main_engine):
    """每個 test 獨立的主庫 session，test 結束後 rollback。"""
    conn = main_engine.connect()
    txn = conn.begin()
    Session = sessionmaker(bind=conn, expire_on_commit=False)
    session = Session()
    yield session
    session.close()
    txn.rollback()
    conn.close()


@pytest.fixture
def scheduler_db(scheduler_engine):
    """每個 test 獨立的 Scheduler DB session。"""
    conn = scheduler_engine.connect()
    txn = conn.begin()
    Session = sessionmaker(bind=conn, expire_on_commit=False)
    session = Session()
    yield session
    session.close()
    txn.rollback()
    conn.close()


# ── SQS Fixtures ─────────────────────────────────────────

@pytest.fixture(scope="session")
def sqs_client():
    """真實 LocalStack SQS client。"""
    import boto3

    return boto3.client(
        "sqs",
        region_name=os.environ.get("DASS_AWS_REGION", "us-east-1"),
        endpoint_url=os.environ.get("DASS_SQS_ENDPOINT_URL", "http://localhost:4566"),
        aws_access_key_id=os.environ.get("DASS_AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("DASS_AWS_SECRET_ACCESS_KEY", "test"),
    )


@pytest.fixture
def purge_queues(sqs_client):
    """每個 test 前清空 queue，避免殘留訊息干擾。"""
    queue_names = [
        os.environ.get("DASS_QUEUE_NAME", "dass-tasks"),
        os.environ.get("DASS_QUEUE_NAME_NORMAL", "dass-tasks-normal"),
        os.environ.get("DASS_QUEUE_NAME_RETRY", "dass-tasks-retry"),
    ]
    for name in queue_names:
        try:
            url = sqs_client.get_queue_url(QueueName=name)["QueueUrl"]
            sqs_client.purge_queue(QueueUrl=url)
        except Exception:
            pass
    yield


# ── Helper Factories ─────────────────────────────────────

@pytest.fixture
def make_job(main_db):
    """建立一個 Job 的 helper factory。"""

    def _make(**overrides):
        job = Job(
            id=str(uuid4()),
            name=overrides.get("name", f"integ-job-{uuid4().hex[:8]}"),
            cron_expression=overrides.get("cron_expression", "* * * * *"),
            action_type=overrides.get("action_type", "http"),
            action_config=overrides.get(
                "action_config",
                {
                    "method": "GET",
                    "url": "https://httpbin.org/get",
                    "timeout_seconds": 5,
                    "headers": {},
                },
            ),
            enabled=overrides.get("enabled", True),
            concurrency_policy=overrides.get("concurrency_policy", "allow"),
            max_retries=overrides.get("max_retries", 0),
            next_fire_at=overrides.get(
                "next_fire_at", datetime.now(UTC) - timedelta(seconds=1)
            ),
        )
        main_db.add(job)
        main_db.flush()
        return job

    return _make


@pytest.fixture
def make_task(main_db):
    """建立一個 Task 的 helper factory。"""

    def _make(job_id: str, **overrides):
        task = Task(
            job_id=job_id,
            status=overrides.get("status", "pending"),
            trigger_type=overrides.get("trigger_type", "manual"),
            retry_count=overrides.get("retry_count", 0),
        )
        main_db.add(task)
        main_db.flush()
        return task

    return _make
