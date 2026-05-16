from datetime import datetime, UTC, timedelta

from app.models.job import Job
from app.repositories.job_repository import JobRepository

def test_due_jobs_logic(db_session):
    """測試 JobRepository 的心臟：精準找出到期的任務"""
    repo = JobRepository(db_session)
    now = datetime.now(UTC)
    
    # Arrange: 準備四種不同情境的 Job 藍圖 (補上必填的 cron_expression)
    
    # 1. Happy Path: 正常到期且啟用 (應該要抓到)
    job_normal = Job(name="due_job", cron_expression="* * * * *", action_type="http", action_config={}, concurrency_policy="allow", max_retries=0, 
                     enabled=True, next_fire_at=now - timedelta(minutes=1))
    
    # 2. Sad Path: 雖然到期了，但是被停用 (不該抓到！)
    job_disabled = Job(name="disabled_job", cron_expression="* * * * *", action_type="http", action_config={}, concurrency_policy="allow", max_retries=0, 
                       enabled=False, next_fire_at=now - timedelta(minutes=1))
    
    # 3. Edge Case: 還差一分鐘才到期 (不該抓到！)
    job_future = Job(name="future_job", cron_expression="* * * * *", action_type="http", action_config={}, concurrency_policy="allow", max_retries=0, 
                     enabled=True, next_fire_at=now + timedelta(minutes=1))
               
    # 4. Edge Case: 剛好就是現在這一秒到期 (因為是 <=，所以應該要抓到)
    job_exact_now = Job(name="exact_now_job", cron_expression="* * * * *", action_type="http", action_config={}, concurrency_policy="allow", max_retries=0, 
                        enabled=True, next_fire_at=now)
               
    # 一口氣把四張藍圖存進空資料庫
    db_session.add_all([job_normal, job_disabled, job_future, job_exact_now])
    db_session.commit()
    
    # Act: 呼叫你寫的 due_jobs 方法
    due_jobs = repo.due_jobs(now)
    
    # Assert: 裁判驗證
    # 總共 4 個任務，但只有正常到期和剛好現在到期的 2 個可以被抓出來
    assert len(due_jobs) == 2
    
    # 嚴格確認抓出來的是哪兩個 (提取它們的 ID 來比對)
    due_job_ids = [j.id for j in due_jobs]
    assert job_normal.id in due_job_ids
    assert job_exact_now.id in due_job_ids