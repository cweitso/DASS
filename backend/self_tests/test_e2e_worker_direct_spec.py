import os
import pytest
from datetime import datetime

from app.models.job import Job
from app.models.task import Task
from app.queue.memory import MemoryQueueClient
from app.services.worker_service import WorkerService
from app.utils.time import utcnow


def test_worker_end_to_end_container_execution(db_session, capsys):
    """
    這是一個 E2E 測試：
    1. 在虛擬 DB (db_session) 中插入符合 ContainerSpec 定義的 Job 與 Task。
    2. 使用 alpine image 來執行一個簡單的 echo 命令。
    3. Worker claim 任務、從 DB 載入 container_spec，並透過 ExecutionService 執行 `docker run`。
    4. 驗證最終成功回寫狀態，並觀察輸出。
    """
    print("\n--- [1] 準備虛擬資料庫與測試資料 ---")
    
    # 建立一個測試 Job，模擬直接存入的 ContainerSpec
    job = Job(
        name="test-worker-e2e-job",
        cron_expression="* * * * *",
        action_type="container",  
        action_config={}, 
        runtime_spec={
            "image": "alpine:latest",
            "command": ["echo", "worker testing container spec execution!"],
            "env": {"TEST_VAR": "HELLO_WORKER"},
            "timeout_seconds": 30,
            "cpu": 0.5,
            "memory_mb": 128
        },
        concurrency_policy="allow",
        max_retries=0,
        next_fire_at=utcnow(),
        enabled=True
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    print(f"✅ 成功建立 Job (ID: {job.id})")

    # 建立一個初始狀態為 pending 的 Task
    task = Task(
        job_id=job.id,
        status="pending",
        trigger_type="manual"
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    print(f"✅ 成功建立 pending Task (ID: {task.id})")

    print("\n--- [2] 初始化 WorkerService 準備處理排程 ---")
    queue_client = MemoryQueueClient()
    worker = WorkerService(
        db=db_session,
        queue_client=queue_client,
        worker_id="test-e2e-worker"
    )
    
    # 在這裡讓 worker 將任務抓出來處理
    print(f"⏳ Worker 開始嘗試執行 task (ID: {task.id})... 這會啟動 docker run")
    success = worker.process_task_id(str(task.id))

    print("\n--- [3] 執行完畢，驗證並且觀測結果 ---")
    # 重新從 DB 拿取更新後的 Task
    db_session.expire_all()
    finished_task = db_session.query(Task).filter(Task.id == task.id).first()

    print(f"Worker process_task_id 方法執行回饋: {success}")
    print(f"Task 最終狀態: {finished_task.status}")
    print(f"Task Started At: {finished_task.started_at}")
    print(f"Task Finished At: {finished_task.finished_at}")
    print(f"Task Stdout:\n{finished_task.stdout}")
    print(f"Task Stderr:\n{finished_task.stderr}")

    # 以 Pytest assertions 確認測試符合預期
    assert success is True
    assert finished_task.status == "success"
    # 確認 alpine echo 有成功輸出在 stdout 之中
    assert "worker testing container spec execution!" in finished_task.stdout

    print("🎉 測試完全通過！Worker 已成功實作分離的 Container Spec 執行機制。")
