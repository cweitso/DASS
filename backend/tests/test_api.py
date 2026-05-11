from __future__ import annotations


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


def test_list_jobs(client):
    create_response = client.post(
        "/api/v1/jobs",
        json={
            "name": "job-list-item",
            "cron_expression": "* * * * *",
            "action_type": "http",
            "action_config": {"method": "GET", "url": "https://example.com", "timeout_seconds": 5, "headers": {}},
            "enabled": True,
            "concurrency_policy": "allow",
            "max_retries": 0,
        },
    )
    assert create_response.status_code == 200

    response = client.get("/api/v1/jobs")
    assert response.status_code == 200
    jobs = response.json()
    assert len(jobs) == 1
    assert jobs[0]["name"] == "job-list-item"
    assert isinstance(jobs[0]["id"], str)
