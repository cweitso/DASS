"""Migration-graph sanity — guards the recurring 'multiple alembic heads' class of bug.

These run offline (no DB): they only inspect the revision graph in alembic/versions.
"""
from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def _script_dir() -> ScriptDirectory:
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_single_alembic_head():
    heads = _script_dir().get_heads()
    assert len(heads) == 1, f"expected exactly one migration head, found {heads}"


def test_revision_history_is_linear():
    # 線性歷史：沒有 merge revision（down_revision 為 tuple 代表分支合併）。
    for rev in _script_dir().walk_revisions():
        assert not isinstance(rev.down_revision, tuple), (
            f"revision {rev.revision} merges branches: down_revision={rev.down_revision!r}"
        )
