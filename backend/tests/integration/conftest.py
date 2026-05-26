"""Integration test fixtures — real PostgreSQL + LocalStack SQS."""
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


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: requires Docker services")


@pytest.fixture(scope="session")
def main_engine():
    url = os.environ.get(
        "DASS_DATABASE_URL",
        "postgresql+psycopg://dass:dass@127.0.0.1:5432/dass",
    )
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def scheduler_engine():
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
    conn = scheduler_engine.connect()
    txn = conn.begin()
    Session = sessionmaker(bind=conn, expire_on_commit=False)
    session = Session()
    yield session
    session.close()
    txn.rollback()
    conn.close()


@pytest.fixture(scope="session")
def sqs_client():
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
    """Drain (not purge) all dass queues until empty.

    AWS doc / LocalStack: `purge_queue` is async — messages sent up to 60s after
    a purge call may also get swept. Tests that purge then immediately send a
    new message hit this race. Draining is synchronous: when this fixture yields
    the queue is verifiably empty, and any subsequent send is safe.
    """
    queue_names = [
        os.environ.get("DASS_QUEUE_NAME", "dass-tasks"),
        os.environ.get("DASS_QUEUE_NAME_NORMAL", "dass-tasks-normal"),
        os.environ.get("DASS_QUEUE_NAME_RETRY", "dass-tasks-retry"),
    ]
    import botocore.exceptions

    for name in queue_names:
        try:
            url = sqs_client.get_queue_url(QueueName=name)["QueueUrl"]
        except botocore.exceptions.ClientError:
            continue
        while True:
            resp = sqs_client.receive_message(
                QueueUrl=url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=0,
            )
            messages = resp.get("Messages", [])
            if not messages:
                break
            for m in messages:
                sqs_client.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
    yield


@pytest.fixture
def make_job(main_db):
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
            # S4: WorkerService._execute_job 需要 runtime_spec 才能 build ContainerSpec；
            # 不給的話 ContainerSpec(**{}) 會在 image 缺失時噴 TypeError，executor 永遠跑不到
            runtime_spec=overrides.get(
                "runtime_spec",
                {"image": "alpine:3", "command": ["true"], "env": {}, "timeout_seconds": 1},
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
