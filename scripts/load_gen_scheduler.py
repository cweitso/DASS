#!/usr/bin/env python3
import asyncio
import uuid
from datetime import datetime, UTC, timedelta
from sqlalchemy import insert
from app.models.job import Job
from app.db.session import SessionLocal


def main():
    print("Creating 500 high-frequency test jobs...")
    jobs_data = []
    now = datetime.now(UTC)
    run_id = uuid.uuid4().hex[:6]

    for i in range(500):
        jobs_data.append(
            {
                "id": uuid.uuid4().hex,
                "name": f"stress-test-{run_id}-{i}",
                "cron_expression": "* * * * *",  # every minute
                "next_fire_at": now
                + timedelta(seconds=i * 30),  # stagger first fire by 30s per job
                "action_type": "shell",
                "action_config": {"command": "echo stress"},
                "enabled": True,
                "job_type": "scheduled",
                "concurrency_policy": "Allow",
            }
        )

    print("Writing to the database...")
    with SessionLocal() as db:
        db.execute(insert(Job), jobs_data)
        db.commit()
    print("Done.")


if __name__ == "__main__":
    main()
