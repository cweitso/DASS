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

