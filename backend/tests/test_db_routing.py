"""Unit tests for RoutingSession.get_bind — the read/write split that has churned
repeatedly (replica-lag safeguards). Engines are monkeypatched to sentinels so we
assert routing decisions, not real connections.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import session as dbsession
from app.models.job import Job


@pytest.fixture
def routing(monkeypatch):
    def _make(replica_ok: bool = True):
        monkeypatch.setattr(dbsession, "primary_engine", "PRIMARY")
        monkeypatch.setattr(dbsession, "replica_engine", "REPLICA")
        monkeypatch.setattr(dbsession, "_replica_available", lambda: replica_ok)
        return dbsession.RoutingSession()

    return _make


def test_select_routes_to_replica_when_healthy(routing):
    sess = routing(replica_ok=True)
    assert sess.get_bind(clause=select(Job)) == "REPLICA"


def test_select_falls_back_to_primary_when_replica_down(routing):
    sess = routing(replica_ok=False)
    assert sess.get_bind(clause=select(Job)) == "PRIMARY"


def test_force_primary_flag_pins_reads_to_primary(routing):
    sess = routing(replica_ok=True)
    sess.info["force_primary"] = True
    assert sess.get_bind(clause=select(Job)) == "PRIMARY"


def test_non_select_clause_defaults_to_primary(routing):
    sess = routing(replica_ok=True)
    assert sess.get_bind(clause=None) == "PRIMARY"
