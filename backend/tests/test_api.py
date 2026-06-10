from __future__ import annotations

from app.models.job import Job
from app.models.task import Task


def test_create_job(client):
    response = client.post(
        "/api/v1/jobs",
        json={
            "name": "job-a",
            "cron_expression": "* * * * *",
            "action_type": "http",
            "action_config": {"method": "GET", "url": "https://example.com", "timeout_seconds": 5, "headers": {}},
            "enabled": True,
            "concurrency_policy": "allow",
            "max_retries": 1,
        },
    )
    assert response.status_code == 200
    assert response.json()["name"] == "job-a"


def test_create_job_without_cron_becomes_one_time_job(client, db_session):
    response = client.post(
        "/api/v1/jobs",
        json={
            "name": "job-one-time",
            "cron_expression": None,
            "action_type": "shell",
            "action_config": {"command": "echo one-time", "timeout_seconds": 5},
            "enabled": True,
            "concurrency_policy": "allow",
            "max_retries": 0,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cron_expression"] is None
    assert body["next_fire_at"] is None

    job = db_session.query(Job).filter_by(name="job-one-time").one()
    assert job.job_type == "normal"
    assert job.cron_expression is None
    assert job.next_fire_at is None

    task = db_session.query(Task).filter_by(job_id=job.id).one()
    assert task.status == "pending"
    # 建立即執行的隨需觸發 → manual（scheduler 沒參與，不可標 scheduled）。
    assert task.trigger_type == "manual"

    messages = client.app.state.normal_queue_client.receive_tasks(max_messages=1, wait_time_seconds=0)
    assert len(messages) == 1


def test_list_jobs(client):
    payloads = [
        {
            "name": "job-list-item-a",
            "cron_expression": "* * * * *",
            "action_type": "http",
            "action_config": {"method": "GET", "url": "https://example.com/a", "timeout_seconds": 5, "headers": {}},
            "enabled": True,
            "concurrency_policy": "allow",
            "max_retries": 0,
        },
        {
            "name": "job-list-item-b",
            "cron_expression": "* * * * *",
            "action_type": "shell",
            "action_config": {"command": "echo b", "timeout_seconds": 5},
            "enabled": False,
            "concurrency_policy": "forbid",
            "max_retries": 1,
        },
    ]

    for payload in payloads:
        create_response = client.post("/api/v1/jobs", json=payload)
        assert create_response.status_code == 200

    response = client.get("/api/v1/jobs")
    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert body["total"] == 2
    assert body["total_pages"] == 1
    assert len(body["items"]) == 2
    assert {item["name"] for item in body["items"]} == {
        "job-list-item-a",
        "job-list-item-b",
    }
    assert isinstance(body["items"][0]["id"], str)


def test_list_jobs_supports_filters_and_pagination(client):
    client.post(
        "/api/v1/jobs",
        json={
            "name": "filter-enabled-http",
            "cron_expression": "* * * * *",
            "action_type": "http",
            "action_config": {"method": "GET", "url": "https://example.com", "timeout_seconds": 5, "headers": {}},
            "enabled": True,
            "concurrency_policy": "allow",
            "max_retries": 0,
        },
    )
    client.post(
        "/api/v1/jobs",
        json={
            "name": "filter-disabled-shell",
            "cron_expression": "* * * * *",
            "action_type": "shell",
            "action_config": {"command": "echo hi", "timeout_seconds": 5},
            "enabled": False,
            "concurrency_policy": "forbid",
            "max_retries": 0,
        },
    )
    client.post(
        "/api/v1/jobs",
        json={
            "name": "filter-enabled-shell",
            "cron_expression": "* * * * *",
            "action_type": "shell",
            "action_config": {"command": "echo hi", "timeout_seconds": 5},
            "enabled": True,
            "concurrency_policy": "replace",
            "max_retries": 0,
        },
    )

    response = client.get("/api/v1/jobs", params={"enabled": "true", "action_type": "shell", "page_size": 1, "page": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["total_pages"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "filter-enabled-shell"

    response = client.get("/api/v1/jobs", params={"q": "filter", "page_size": 2, "page": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["total_pages"] == 2
    assert len(body["items"]) == 2


def test_update_job_can_clear_cron_expression(client, db_session):
    create_response = client.post(
        "/api/v1/jobs",
        json={
            "name": "job-to-clear",
            "cron_expression": "*/5 * * * *",
            "action_type": "shell",
            "action_config": {"command": "echo hi", "timeout_seconds": 5},
            "enabled": True,
            "concurrency_policy": "allow",
            "max_retries": 0,
        },
    )
    assert create_response.status_code == 200
    job_id = create_response.json()["id"]

    update_response = client.put(
        f"/api/v1/jobs/{job_id}",
        json={"cron_expression": None},
    )
    assert update_response.status_code == 200
    body = update_response.json()
    assert body["cron_expression"] is None
    assert body["next_fire_at"] is None

    job = db_session.query(Job).filter_by(id=job_id).one()
    assert job.job_type == "normal"
    assert job.cron_expression is None
    assert job.next_fire_at is None


def test_get_task_details(client, db_session):
    job = Job(
        name="task-detail-job",
        job_type="scheduled",
        cron_expression="*/5 * * * *",
        action_type="shell",
        action_config={"command": "echo hi", "timeout_seconds": 5},
        runtime_spec={"image": "alpine:3", "command": ["sh", "-c", "echo hi"], "env": {}, "timeout_seconds": 5},
        enabled=True,
        concurrency_policy="allow",
        max_retries=1,
        next_fire_at=None,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    task = Task(
        job_id=job.id,
        status="success",
        trigger_type="manual",
        retry_count=2,
        stdout="job output",
        stderr="",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    response = client.get(f"/api/v1/tasks/{task.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(task.id)
    assert body["job_id"] == str(job.id)
    assert body["status"] == "success"
    assert body["stdout"] == "job output"
    assert body["stderr"] == ""
