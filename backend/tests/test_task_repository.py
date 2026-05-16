from datetime import datetime, UTC, timedelta

from app.models.job import Job
from app.models.task import Task
from app.repositories.task_repository import TaskRepository

import concurrent.futures

def _create_dummy_job(db_session):
    """輔助函式：因為 Task 有 Foreign Key 規定必須綁定一個 Job，我們先偷偷塞一個假 Job 進資料庫"""
    job = Job(
        name="test-job-for-task", 
        cron_expression="* * * * *", 
        action_type="http", 
        action_config={"method": "GET", "url": "http://test"},
        concurrency_policy="allow",
        max_retries=0,
        next_fire_at=datetime.now(UTC)
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def test_create_task(db_session):
    """測試 Level 1：基礎的 create 方法"""
    repo = TaskRepository(db_session)
    job = _create_dummy_job(db_session)
    
    # 1. 準備一張假工單
    new_task = Task(job_id=job.id, status="pending", trigger_type="manual")
    
    # 2. 呼叫你寫的函式
    created_task = repo.create(new_task)
    
    # 3. 裁判驗證 (Assert)
    assert created_task.id is not None # 檢查資料庫有沒有發給它流水號
    assert created_task.status == "pending"
    assert created_task.job_id == job.id


def test_claim_pending_success(db_session):
    """測試 Level 3：原子性搶工單機制"""
    repo = TaskRepository(db_session)
    job = _create_dummy_job(db_session)
    
    # 1. 先在資料庫放一張還沒被搶的 pending 工單
    task = Task(job_id=job.id, status="pending", trigger_type="scheduled")
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    
    # 2. 假裝有個 Worker 叫 worker-999 來搶單
    locked_until = datetime.now(UTC) + timedelta(minutes=5)
    claimed_task = repo.claim_pending(str(task.id), "worker-999", locked_until)
    
    # 3. 裁判驗證 (Assert)
    assert claimed_task is not None             # 檢查有沒有搶成功 (不是回傳 None)
    assert claimed_task.status == "running"     # 檢查狀態有沒有被改成 running
    assert claimed_task.locked_by == "worker-999" # 檢查署名是不是這個 worker

  
def test_claim_pending_fail_if_already_claimed(db_session):
    """測試 Level 3 異常路徑：工單已經被別人搶走了"""
    repo = TaskRepository(db_session)
    job = _create_dummy_job(db_session)
    
    # 1. Arrange (準備)：偷偷放一張 "已經是 running" 的工單 (模擬已經被 worker-111 搶走了)
    task = Task(job_id=job.id, status="running", locked_by="worker-111", trigger_type="scheduled")
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    
    # 2. Act (執行)：晚了一步的 worker-999，試圖去搶這張單
    locked_until = datetime.now(UTC) + timedelta(minutes=5)
    claimed_task = repo.claim_pending(str(task.id), "worker-999", locked_until)
    
    # 3. Assert (驗證)：因為狀態不是 pending，WHERE 條件找不到東西，rowcount 會是 0，應該要回傳 None
    assert claimed_task is None
    
    
def test_claim_pending_concurrency_race(db_session):
    """測試 Level 3 最高機密：模擬 5 個 Worker 瞬間併發搶單"""
    repo = TaskRepository(db_session)
    job = _create_dummy_job(db_session)
    
    # 1. 舞台準備：放一張熱騰騰的 pending 工單
    task = Task(job_id=job.id, status="pending", trigger_type="scheduled")
    db_session.add(task)
    db_session.commit()
    task_id = str(task.id)
    locked_until = datetime.now(UTC) + timedelta(minutes=5)
    
    # 2. 定義 Worker 的搶單動作
    def worker_rush(worker_name):
        try:
            # 每個 worker 都去呼叫同一個 repo 的 claim_pending
            return repo.claim_pending(task_id, worker_name, locked_until)
        except Exception as e:
            # 如果資料庫因為併發鎖死拋出例外，我們也視為沒搶到
            return None

    # 3. Act：打開多執行緒，讓 5 個 Worker 「同時」衝線！
    worker_names = ["worker-A", "worker-B", "worker-C", "worker-D", "worker-E"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        # map 會同時派發 5 個任務，並把結果收集成一個 list
        results = list(executor.map(worker_rush, worker_names))
        
    # 4. Assert：結算戰果
    # 把有搶到 Task 的結果過濾出來 (不是 None 的)
    success_claims = [r for r in results if r is not None]
    
    # 💡 見證奇蹟的時刻：5 個人進去搶，必須「剛好只有 1 個人」活著帶著 Task 出來！
    assert len(success_claims) == 1
    # 確認搶到的那個人，狀態真的變成了 running
    assert success_claims[0].status == "running"
    # 確認是 A~E 其中一個人搶到的
    assert success_claims[0].locked_by in worker_names
    
    
def test_list_expired_running_edge_cases(db_session):
    """測試 Level 2.5：孤兒工單清道夫的邊界情況與異常路徑"""
    repo = TaskRepository(db_session)
    job = _create_dummy_job(db_session)
    now = datetime.now(UTC)
    
    # 陷阱 1 (Edge Case)：剛好過期 1 秒鐘的工單 (應該要被清道夫抓出來)
    expired_task = Task(job_id=job.id, status="running", locked_until=now - timedelta(seconds=1), trigger_type="scheduled")
    
    # 陷阱 2 (Edge Case)：還差 1 秒鐘才過期的工單 (絕對不能抓，人家還在跑)
    alive_task = Task(job_id=job.id, status="running", locked_until=now + timedelta(seconds=1), trigger_type="scheduled")
    
    # 陷阱 3 (Sad Path)：雖然過期很久，但狀態已經是 success (可能是上個階段忘記清空鎖定時間，不該抓)
    success_task = Task(job_id=job.id, status="success", locked_until=now - timedelta(days=1), trigger_type="scheduled")
    
    # 陷阱 4 (Edge Case)：剛好 0 秒 (嚴格等於 now)。因為我們用 <，所以它應該要算「還活著」，不該被抓！
    exact_zero_task = Task(job_id=job.id, status="running", locked_until=now, trigger_type="scheduled")
    
    db_session.add_all([expired_task, alive_task, success_task, exact_zero_task])
    db_session.commit()
    
    # Act: 呼叫清道夫
    orphans = repo.list_expired_running(now)
    
    # Assert: 裁判驗證
    assert len(orphans) == 1                   # 必須精準地只抓到 1 個
    assert orphans[0].id == expired_task.id    # 且抓到的必須是 expired_task
    
    
def test_mark_failed_normal_path(db_session):
    """測試 mark_failed：一般失敗路徑 (final=False)"""
    repo = TaskRepository(db_session)
    job = _create_dummy_job(db_session)
    
    # 1. Arrange: 準備一張正在執行中 (running) 且被鎖定的工單
    task = Task(job_id=job.id, status="running", locked_by="worker-1", trigger_type="manual")
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    
    # 2. Act: 標記為一般失敗
    updated_task = repo.mark_failed(task, stdout="some log", stderr="error trace", final=False)
    
    # 3. Assert: 驗證狀態與清理邏輯
    assert updated_task.status == "failed"          # 狀態應為 failed
    assert updated_task.locked_by is None           # 鎖定者必須清空
    assert updated_task.locked_until is None        # 鎖定時間必須清空
    assert updated_task.finished_at is not None     # 必須有結束時間
    assert updated_task.stderr == "error trace"     # 錯誤訊息有正確存入


def test_mark_failed_final_path(db_session):
    """測試 mark_failed：最終失敗路徑 (final=True)"""
    repo = TaskRepository(db_session)
    job = _create_dummy_job(db_session)
    
    # 1. Arrange: 準備一張執行中的工單
    task = Task(job_id=job.id, status="running", locked_by="worker-1", trigger_type="manual")
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    
    # 2. Act: 標記為最終失敗
    updated_task = repo.mark_failed(task, stdout="logs", stderr="fatal error", final=True)
    
    # 3. Assert: 驗證狀態
    assert updated_task.status == "final_failed"    # 狀態應為 final_failed
    assert updated_task.locked_by is None           # 同樣需要清理鎖定