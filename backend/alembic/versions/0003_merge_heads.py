"""merge 0002_job_runtime_spec and 508a085e1e1f

Revision ID: 0003_merge_heads
Revises: 0002_job_runtime_spec, 508a085e1e1f
Create Date: 2026-05-27

"""
from alembic import op

revision = "0003_merge_heads"
down_revision = ("0002_job_runtime_spec", "508a085e1e1f")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
