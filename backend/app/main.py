from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1.jobs import router as jobs_router
from app.api.v1.tasks import router as tasks_router
from app.api.v1.vms import router as vms_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(title="dass API", version="0.1.0")
origins = ["*"] if settings.cors_origins == "*" else [origin.strip() for origin in settings.cors_origins.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(jobs_router)
app.include_router(tasks_router)
app.include_router(vms_router)


@app.get("/health")
def health():
    """Health check endpoint：確認 DB 連線正常。
    #   1. 用 SessionLocal() 開 session
    #   2. 執行 SELECT 1 確認 DB 連線
    #   3. 回傳 {"status": "ok", "service": "dass"}
    """
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ok", "service": "dass"}

@app.get("/metrics")
def metrics():
    """回傳 Job 和 Task 的統計數字。
    #   1. 用 SessionLocal() 開 session
    #   2. SELECT count(*) FROM jobs
    #   3. SELECT count(*) FROM tasks
    #   4. 回傳 {"jobs": int, "tasks": int}
    """
    with SessionLocal() as db:
        num_jobs = db.execute(text("SELECT count(*) FROM jobs")).scalar()
        num_tasks = db.execute(text("SELECT count(*) FROM tasks")).scalar()
    return {"jobs": num_jobs, "tasks": num_tasks}
