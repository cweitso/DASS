#!/usr/bin/env python3
import uuid
import time
from app.models.job import Job
from app.models.task import Task
from app.db.session import SessionLocal
from app.queue.factory import get_normal_queue_client


def create_normal_job(db, name: str, downstream_jobs: list[Job] = None) -> Job:
    """直接寫入資料庫建立一個沒有定時機制的 Normal Job"""
    job_id = uuid.uuid4()  # 必須傳入 UUID 物件，不然 SQLAlchemy ORM 會對比失敗
    job = Job(
        id=job_id,
        name=name,
        action_type="shell",
        action_config={"command": f"echo 'Executing {name}' && sleep 2"},
        runtime_spec={
            "image": "alpine:3.20",
            "command": ["sh", "-c", f"echo 'Executing {name}' && sleep 2"],
        },
        enabled=True,
        concurrency_policy="allow",
        max_retries=0,
        job_type="normal",  # 改為 normal
        cron_expression=None,  # 沒有 cron
        next_fire_at=None,
    )

    if downstream_jobs:
        job.downstream_jobs.extend(downstream_jobs)

    db.add(job)
    print(f"[SUCCESS] Created {name} with ID: {job_id}")
    return job


def main():
    print("=== Start creating C <- A, B (Many-to-One DAG Job) ===")

    with SessionLocal() as db:
        # 1. 建立 Job C (下游)
        job_c = create_normal_job(db, name=f"Normal-Job-C-{int(time.time())}")

        # 2. 建立 Job A 和 Job B (上游)，並把它們的下游都指向 Job C
        job_a = create_normal_job(
            db, name=f"Normal-Job-A-{int(time.time())}", downstream_jobs=[job_c]
        )
        job_b = create_normal_job(
            db, name=f"Normal-Job-B-{int(time.time())}", downstream_jobs=[job_c]
        )

        db.commit()

        print(f"\nManually triggering Job A ({job_a.id}) and Job B ({job_b.id})...")

        # 建立 A 和 B 的 Task
        task_a = Task(
            job_id=str(job_a.id), status="pending", trigger_type="manual", retry_count=0
        )
        task_b = Task(
            job_id=str(job_b.id), status="pending", trigger_type="manual", retry_count=0
        )
        db.add(task_a)
        db.add(task_b)
        db.commit()

        # 將 Task IDs 推送給 Worker
        from app.core.config import get_settings

        settings = get_settings()
        settings.sqs_endpoint_url = "http://localhost:4566"

        queue = get_normal_queue_client()
        queue.send_task(str(task_a.id))
        queue.send_task(str(task_b.id))
        print(f"[PUSHED] Task A (ID: {task_a.id}) is pushed to Worker!")
        print(f"[PUSHED] Task B (ID: {task_b.id}) is pushed to Worker!")

        print(
            "\n[WAITING] Waiting for Worker to finish and checking database for Job C..."
        )

        # 這裡不只是印出 A 跟 B，我們期待 Scheduler 會自動觸發 C
        for _ in range(15):
            time.sleep(1)
            # 檢查 C 是否被觸發了
            tasks_c = db.query(Task).filter(Task.job_id == str(job_c.id)).all()
            if tasks_c:
                task_c = tasks_c[0]
                print(
                    f"[SUCCESS] Scheduler automatically triggered Job C! (Task ID: {task_c.id}, Status: {task_c.status})"
                )
                if task_c.status in ("success", "failed", "final_failed"):
                    print(f"Task C stdout:\n{task_c.stdout}")
                    break
            else:
                db.refresh(task_a)
                db.refresh(task_b)
                print(f"Status - Task A: {task_a.status}, Task B: {task_b.status}")

    print("\n[DONE] Script finished.")


if __name__ == "__main__":
    main()
