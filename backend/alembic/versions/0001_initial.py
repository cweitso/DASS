"""initial

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-29
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("cron_expression", sa.String(length=255), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("action_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("concurrency_policy", sa.String(length=32), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("name", name="uq_jobs_name"),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("job_id", sa.String(length=36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_by", sa.String(length=255), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stdout", sa.Text(), nullable=True),
        sa.Column("stderr", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_jobs_next_fire_at", "jobs", ["next_fire_at"])
    op.create_index("ix_jobs_enabled", "jobs", ["enabled"])
    op.create_index("ix_tasks_job_id", "tasks", ["job_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_locked_until_running", "tasks", ["locked_until"], postgresql_where=sa.text("status = 'running'"))
    op.add_column('jobs', sa.Column('job_type', sa.String(length=32), nullable=False))
    op.alter_column('jobs', 'cron_expression',
               existing_type=sa.VARCHAR(length=255),
               nullable=True)
    op.alter_column('jobs', 'next_fire_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               nullable=True)
    op.create_index(op.f('ix_jobs_job_type'), 'jobs', ['job_type'], unique=False)


def downgrade() -> None:
    op.drop_index("ix_tasks_locked_until_running", table_name="tasks")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_index("ix_tasks_job_id", table_name="tasks")
    op.drop_index("ix_jobs_enabled", table_name="jobs")
    op.drop_index("ix_jobs_next_fire_at", table_name="jobs")
    op.drop_table("tasks")
    op.drop_table("jobs")
    op.drop_index(op.f('ix_jobs_job_type'), table_name='jobs')
    op.alter_column('jobs', 'next_fire_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               nullable=False)
    op.alter_column('jobs', 'cron_expression',
               existing_type=sa.VARCHAR(length=255),
               nullable=False)
    op.drop_column('jobs', 'job_type')

