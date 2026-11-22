"""Observability: trace context on jobs and outbox rows, queue time on attempts, worker presence.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17

- jobs.traceparent: the W3C traceparent of the span that created the job (the job's trace).
- outbox.traceparent: the span that enqueued the message (job creation, a failed attempt, a
  recovery action, a manual retry); the dispatcher continues it and passes it to the worker.
- job_attempts.queued_at: when the job became claimable before this attempt (queued, or its
  retry came due), so queue wait = started_at - queued_at.
- worker_heartbeats: one row per worker process, refreshed every few seconds, with counters
  only that process can observe (fenced attempts, failed heartbeat beats, dead heartbeat
  threads).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("jobs", sa.Column("traceparent", sa.Text(), nullable=True))
    op.add_column("outbox", sa.Column("traceparent", sa.Text(), nullable=True))
    op.add_column("job_attempts", sa.Column("queued_at", TIMESTAMPTZ, nullable=True))
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("started_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("stopped_at", TIMESTAMPTZ, nullable=True),
        sa.Column("fenced_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("heartbeat_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("heartbeat_thread_deaths", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("worker_id", name=op.f("pk_worker_heartbeats")),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
    op.drop_column("job_attempts", "queued_at")
    op.drop_column("outbox", "traceparent")
    op.drop_column("jobs", "traceparent")
