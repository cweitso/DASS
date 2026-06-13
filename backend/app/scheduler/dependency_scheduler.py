from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.task import Task
from app.repositories.job_repository import JobRepository
from app.repositories.task_repository import TaskRepository

logger = logging.getLogger(__name__)


class DependencyScheduler:
    def __init__(self, session_maker, queue_client):
        self.session_maker = session_maker
        self.queue = queue_client

    def trigger_dependent_jobs(self) -> int:
        """輪詢找出剛成功的任務，並觸發它們的下游"""
        counter = 0
        with self.session_maker() as db:
            task_repo = TaskRepository(db)
            job_repo = JobRepository(db)

            # 1. 撈出剛成功但還沒處理相依性的 Tasks
            tasks = task_repo.get_unprocessed_successful_tasks()

            for task in tasks:
                # 2. 標記為已處理，避免下次輪詢重複撈到
                task_repo.mark_processed_for_chaining(task)

                # 3. 取得這個 Task 所屬的 Job
                job = job_repo.get(task.job_id)
                if not job or not job.downstream_jobs:
                    continue  # 如果這個 Job 已經被刪除，或者它根本沒有下游，就跳過

                logger.info(
                    f"[Scheduler] Task {task.id} (Job {job.name}) finished. Checking its {len(job.downstream_jobs)} downstream jobs..."
                )

                # 4. 檢查它的每一個下游任務
                for down_job in job.downstream_jobs:
                    if not down_job.enabled:
                        continue  # 如果下游任務被停用了，就不用管它

                    logger.info(
                        f"[Scheduler] Checking if downstream Job '{down_job.name}' is ready to run..."
                    )
                    all_upstreams_ready = True

                    # 5. 針對這個下游任務，去檢查它的「所有上游」是不是都已經順利完成
                    for up_job in down_job.upstream_jobs:
                        # 從 Task 表撈出這個上游任務的「最新一筆執行紀錄」
                        latest_up_task = (
                            db.query(Task)
                            .filter(Task.job_id == up_job.id)
                            .order_by(Task.created_at.desc())
                            .first()
                        )

                        # 如果連紀錄都沒有，或者最新狀態不是 success，就代表還沒準備好
                        if not latest_up_task or latest_up_task.status != "success":
                            logger.info(
                                f"[Scheduler]   -> Blocked! Upstream Job '{up_job.name}' status is '{latest_up_task.status if latest_up_task else 'None'}'."
                            )
                            all_upstreams_ready = False
                            break  # 只要有一個上游沒過關，就不需要再檢查其他上游了

                    # 6. 如果所有上游都過關了，就發射這個下游任務！
                    if all_upstreams_ready:
                        new_task = Task(
                            job_id=str(down_job.id),
                            status="pending",
                            trigger_type="dependency",
                            retry_count=0,
                        )
                        task_repo.create(new_task)
                        self.queue.send_task(str(new_task.id))
                        counter += 1

                        logger.info(
                            f"[Scheduler]   -> All upstreams ready! Dependency triggered: "
                            f"Job '{job.name}' -> Dispatched downstream Job '{down_job.name}' (New Task: {new_task.id})"
                        )

        return counter
