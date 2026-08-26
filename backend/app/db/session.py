"""Database engines and the read/write routing session.

Two engines back every process: `primary_engine` for writes and read-after-write,
`replica_engine` for plain reads. `RoutingSession` picks between them per statement.

Pool sizes are derived from the process role (DASS_WORKER_ID) because the roles have
very different concurrency: an API server fans out across a thread pool, a worker runs
a handful of tasks, a scheduler runs one loop. Both engines use the same profile so
neither can become the tighter constraint on its own.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import Select, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass(frozen=True)
class PoolProfile:
    size: int
    max_overflow: int
    timeout: int


# Ceiling per process = size + max_overflow, per engine.
_POOL_PROFILES = {
    # FastAPI serves sync endpoints from a ~40-thread pool, so it needs the headroom.
    "api": PoolProfile(size=30, max_overflow=20, timeout=15),
    # A worker runs MAX_CONCURRENT_PER_QUEUE tasks per queue pool; PgBouncer absorbs
    # the fan-out when KEDA scales worker pods out.
    "worker": PoolProfile(size=5, max_overflow=5, timeout=30),
    # Scheduler and autoscaler are single-loop processes that barely touch the DB.
    "default": PoolProfile(size=5, max_overflow=0, timeout=30),
}


def _resolve_role(worker_id: str) -> str:
    """Map DASS_WORKER_ID onto a pool profile.

    Matched as a substring because the value varies by deployment: Compose sets
    "worker"/"scheduler", K8s injects the pod name ("dass-worker-normal-abc12").
    """
    lowered = worker_id.lower()
    if "worker" in lowered:
        return "worker"
    if "api" in lowered:
        return "api"
    return "default"


ROLE = _resolve_role(os.getenv("DASS_WORKER_ID", "api-server"))
_PROFILE = _POOL_PROFILES[ROLE]

# psycopg3 opens server-side prepared statements by default. PgBouncer runs in
# transaction pooling mode and hands the same server connection to different clients,
# where those statement names collide ("prepared statement ... already exists").
# Disabling them costs a little plan caching on direct connections and nothing else.
_CONNECT_ARGS = {"prepare_threshold": None}

primary_engine = create_engine(
    settings.database_url,
    echo=settings.database_echo,
    pool_pre_ping=True,
    pool_size=_PROFILE.size,
    max_overflow=_PROFILE.max_overflow,
    pool_timeout=_PROFILE.timeout,
    connect_args=_CONNECT_ARGS,
)

# A distinct replica is optional. When it is absent — or configured to the same URL —
# reuse the primary engine rather than opening a second pool against the same server,
# which would silently double this process's connection footprint.
_replica_url = settings.replica_database_url
_HAS_REPLICA = bool(_replica_url) and _replica_url != settings.database_url

if _HAS_REPLICA:
    replica_engine = create_engine(
        _replica_url,
        echo=settings.database_echo,
        pool_pre_ping=True,
        pool_size=_PROFILE.size,
        max_overflow=_PROFILE.max_overflow,
        pool_timeout=_PROFILE.timeout,
        # connect_timeout keeps a dead replica from parking the health probe on a
        # 75s TCP SYN retry and hanging every read behind it.
        connect_args={"connect_timeout": 2, **_CONNECT_ARGS},
    )
else:
    replica_engine = primary_engine

# Heartbeats extend task locks from a separate thread/coroutine while a task is
# running. Sharing the primary pool would mean every in-flight task holds two
# connections and a busy worker would exhaust its own pool; this small dedicated
# pool keeps the two workloads from competing.
heartbeat_engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=2,
    max_overflow=8,
    pool_timeout=10,
    connect_args=_CONNECT_ARGS,
)

logger.info(
    "DB engines ready. role=%s pool=%d+%d replica=%s",
    ROLE,
    _PROFILE.size,
    _PROFILE.max_overflow,
    "dedicated" if _HAS_REPLICA else "primary (no replica configured)",
)


# ── Replica liveness ────────────────────────────────────────────────────────────
# Probing before every read would open two connections per query. Probe at most once
# per TTL instead: reads still fall back to the primary when the replica is down, but
# a healthy replica costs nothing.

_REPLICA_HEALTH_TTL_SECONDS = 5.0
_replica_health = {"ok": True, "checked_at": 0.0}


def _replica_available() -> bool:
    if not _HAS_REPLICA:
        return True

    now = time.monotonic()
    if now - _replica_health["checked_at"] < _REPLICA_HEALTH_TTL_SECONDS:
        return _replica_health["ok"]

    try:
        with replica_engine.connect():
            pass
        _replica_health["ok"] = True
    except Exception as exc:  # noqa: BLE001 — any connection error falls back to primary
        if _replica_health["ok"]:
            logger.warning("Replica offline, falling back to primary: %s", exc)
        _replica_health["ok"] = False

    _replica_health["checked_at"] = now
    return _replica_health["ok"]


class RoutingSession(Session):
    """Sends plain SELECTs to the replica and everything else to the primary."""

    def get_bind(self, mapper=None, clause=None, **kw):
        # An ORM flush is a write by definition, whatever the clause looks like.
        if self._flushing:
            return primary_engine

        if not isinstance(clause, Select):
            # DML, DDL, raw text and the no-clause case (Session.get) all belong on
            # the primary. Defaulting here means an unrecognised statement is never
            # routed to a replica by accident.
            return primary_engine

        # Read-after-write guard. `force_primary` is set explicitly by callers that
        # just wrote; a populated identity map means this session already loaded rows
        # it may be about to re-read, and replication lag would surface as stale data
        # or a failed refresh.
        if self.info.get("force_primary") or self.identity_map:
            return primary_engine

        return replica_engine if _replica_available() else primary_engine


SessionLocal = sessionmaker(
    class_=RoutingSession,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


@contextmanager
def force_primary(db: Session) -> Iterator[None]:
    """Pin this session's reads to the primary for the duration of the block.

    Scoped rather than sticky: setting the flag and leaving it set would send every
    later read on a reused session to the primary, quietly disabling the split.
    """
    previous = db.info.get("force_primary")
    had_previous = "force_primary" in db.info
    db.info["force_primary"] = True
    try:
        yield
    finally:
        if had_previous:
            db.info["force_primary"] = previous
        else:
            db.info.pop("force_primary", None)
