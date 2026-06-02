#!/usr/bin/env python3
import asyncio
import uuid
from datetime import datetime, UTC, timedelta
from sqlalchemy import insert
from app.models.job import Job
from app.db.session import SessionLocal


def main():
    print("準備建立 500 個高頻測試 Job...")
    jobs_data = []
    now = datetime.now(UTC)
    run_id = uuid.uuid4().hex[:6]

    for i in range(500):
        jobs_data.append(
            {
                "id": uuid.uuid4().hex,
                "name": f"stress-test-{run_id}-{i}",
                "cron_expression": "* * * * *",  # 5 個星號代表每分鐘一次
                "next_fire_at": now
                + timedelta(seconds=i * 30),  # 每個 Job 延遲 30 秒觸發
                "action_type": "shell",
                "action_config": {"command": "echo stress"},
                "enabled": True,
                "job_type": "scheduled",
                "concurrency_policy": "Allow",
            }
        )

    print("開始寫入資料庫...")
    with SessionLocal() as db:
        db.execute(insert(Job), jobs_data)
        db.commit()
    print("寫入完成！")


if __name__ == "__main__":
    main()
