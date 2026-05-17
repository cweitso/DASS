import pytest
from app.models.job import Job
from app.models.task import Task
from app.queue.memory import MemoryQueueClient
from app.services.worker_service import WorkerService
from app.utils.time import utcnow


def test_api_can_create_multiple_worker_vms(client):
    """
    測試目標 1 & 2：提供 API，能夠一次擴展/創建多台 Worker VMs
    """
    print("\n--- 測試：一次打 API 生出多台 VM ---")
    
    # 呼叫我們剛剛建立的 API，要求產出 3 台 VM
    response = client.post("/vms", json={"count": 3, "instance_type": "t3.medium"})
    
    assert response.status_code == 200
    data = response.json()
    assert len(data["vm_ids"]) == 3
    print(f"API 回傳訊息: {data['message']}")
    print(f"創建出的 VM IDs: {data['vm_ids']}")


def _seed_container_task(db_session, container_name_suffix: str) -> str:
    """Helper：在虛擬資料庫裡面戳出即將要給 VM 跑的 task"""
    job = Job(
        name=f"job-for-{container_name_suffix}",
        cron_expression="* * * * *",
        action_type="container",
        action_config={}, 
        runtime_spec={
            "image": "alpine:latest",
            "command": ["echo", f"Container {container_name_suffix} is running on this VM!"],
            "timeout_seconds": 30
        },
        concurrency_policy="allow",
        max_retries=0,
        next_fire_at=utcnow(),
        enabled=True
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    task = Task(job_id=job.id, status="pending", trigger_type="manual")
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return str(task.id)


def test_worker_vm_executes_one_and_multiple_containers(db_session):
    """
    測試目標 3：一台 Worker VM 上要能夠跑 1 個 Container，或是 2 個 Container，兩種都要測出來。
    """
    print("\n--- 測試：一台 Worker VM 處理多個 Container Tasks ---")
    
    vm_worker_id = "i-abcd1234_worker"
    queue_client = MemoryQueueClient()
    
    # 這是跑在我們建立的 VM 上面的 Worker Service 實例
    worker_vm = WorkerService(
        db=db_session,
        queue_client=queue_client,
        worker_id=vm_worker_id
    )

    # 1. 測試：這台 VM 執行「1 個 Container」
    print("\n[VM 測試情境 A] 此台 VM 執行第一個 Container")
    task1_id = _seed_container_task(db_session, "Number-1")
    
    success1 = worker_vm.process_task_id(task1_id)
    assert success1 is True
    
    db_session.expire_all()
    finished_task1 = db_session.query(Task).filter(Task.id == task1_id).first()
    assert finished_task1.status == "success"
    assert "Container Number-1 is running" in finished_task1.stdout
    print(f"✅ 第一個 Container 執行成功，輸出：{finished_task1.stdout.strip()}")


    # 2. 測試：這台 VM 繼續接力或是平行，執行「第 2 個」以及更多的 Container
    print("\n[VM 測試情境 B] 同一台 VM 接著併發/連續執行另外第 2 個與第 3 個 Container 任務")
    task2_id = _seed_container_task(db_session, "Number-2")
    task3_id = _seed_container_task(db_session, "Number-3")

    # Worker VM 把這兩個任務接起來跑
    success2 = worker_vm.process_task_id(task2_id)
    success3 = worker_vm.process_task_id(task3_id)
    
    assert success2 is True
    assert success3 is True

    db_session.expire_all()
    finished_task2 = db_session.query(Task).filter(Task.id == task2_id).first()
    finished_task3 = db_session.query(Task).filter(Task.id == task3_id).first()
    
    assert finished_task2.status == "success"
    assert "Container Number-2 is running" in finished_task2.stdout
    print(f"✅ 第二個 Container 執行成功，輸出：{finished_task2.stdout.strip()}")

    assert finished_task3.status == "success"
    assert "Container Number-3 is running" in finished_task3.stdout
    print(f"✅ 第三個 Container 執行成功，輸出：{finished_task3.stdout.strip()}")

    print("\n🎉 驗證完畢：該 Worker VM ID '{vm_worker_id}' 可以隨意伸縮執行一到多個 Container Task。")
