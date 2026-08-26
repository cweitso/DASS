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

# Always allowed on top of DASS_CORS_ORIGINS: the browser reaches the API either
# straight on :3000/:8000 or through Traefik's TLS entrypoint on :8443.
_BUILTIN_ORIGINS = (
    "http://dass.localhost",
    "http://localhost",
    "http://127.0.0.1",
    "http://dass.localhost:3000",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://dass.localhost:8443",
    "https://localhost:8443",
    "https://127.0.0.1:8443",
)


def _cors_origins(configured: str) -> list[str]:
    if configured == "*":
        return ["*"]

    origins: list[str] = []
    for origin in (*configured.split(","), *_BUILTIN_ORIGINS):
        origin = origin.strip()
        if origin and origin not in origins:
            origins.append(origin)
    return origins


origins = _cors_origins(settings.cors_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    # Credentials cannot be combined with a wildcard origin.
    allow_credentials=origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(jobs_router)
app.include_router(tasks_router)
app.include_router(vms_router)


@app.get("/health")
def health():
    """Liveness plus a round trip to the database."""
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ok", "service": "dass"}


@app.get("/metrics")
def metrics():
    """Job and task counts. Not Prometheus format — the exporters cover that."""
    with SessionLocal() as db:
        return {
            "jobs": db.execute(text("SELECT count(*) FROM jobs")).scalar(),
            "tasks": db.execute(text("SELECT count(*) FROM tasks")).scalar(),
        }
