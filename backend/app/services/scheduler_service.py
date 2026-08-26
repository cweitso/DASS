from app.scheduler.cron_scheduler import CronScheduler
from app.scheduler.dependency_scheduler import DependencyScheduler


class SchedulerService:
    """Facade over the two independent scheduling strategies.

    Cron dispatches go to the scheduled queue; dependency-triggered runs go to the
    normal queue, so cron backlog and DAG fan-out scale separately.
    """

    def __init__(
        self,
        session_maker,
        scheduled_queue,
        normal_queue,
        worker_visibility_timeout_seconds: int = 300,
    ):
        self.cron_scheduler = CronScheduler(
            session_maker, scheduled_queue, worker_visibility_timeout_seconds
        )
        self.dependency_scheduler = DependencyScheduler(session_maker, normal_queue)

    def sync_jobs(self) -> None:
        self.cron_scheduler.sync_jobs()

    def recover_orphans(self) -> int:
        return self.cron_scheduler.recover_orphans()

    def dispatch_due_jobs(self) -> int:
        return self.cron_scheduler.dispatch_due_jobs()

    def trigger_dependent_jobs(self) -> int:
        return self.dependency_scheduler.trigger_dependent_jobs()
