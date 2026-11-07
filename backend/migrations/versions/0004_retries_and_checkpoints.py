"""Retry budget invariant and stage checkpoints.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-07

- ck_jobs_claimable_has_attempt_left: a job that can be claimed (queued or retry_wait) always has
  an attempt left.
- stage_checkpoints: the files each finished stage wrote, so a retry can resume after the last
  stage whose output still verifies.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)
SHA256_PATTERN = "^[0-9a-f]{64}$"


def upgrade() -> None:
    op.create_check_constraint(
        op.f("ck_jobs_claimable_has_attempt_left"),
        "jobs",
        "status NOT IN ('queued', 'retry_wait') OR attempt_count < max_attempts",
    )
    op.create_table(
        "stage_checkpoints",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("stage_version", sa.SmallInteger(), nullable=False),
        sa.Column("attempt_no", sa.SmallInteger(), nullable=False),
        sa.Column("input_fingerprint", sa.Text(), nullable=False),
        sa.Column("outputs", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("job_id", "stage", name=op.f("pk_stage_checkpoints")),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_stage_checkpoints_job_id_jobs"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "stage_version >= 1", name=op.f("ck_stage_checkpoints_stage_version_positive")
        ),
        sa.CheckConstraint(
            "attempt_no >= 1", name=op.f("ck_stage_checkpoints_attempt_no_positive")
        ),
        sa.CheckConstraint(
            f"input_fingerprint ~ '{SHA256_PATTERN}'",
            name=op.f("ck_stage_checkpoints_input_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "CASE WHEN jsonb_typeof(outputs) = 'array' "
            "THEN jsonb_array_length(outputs) > 0 ELSE false END",
            name=op.f("ck_stage_checkpoints_outputs_nonempty"),
        ),
    )


def downgrade() -> None:
    op.drop_table("stage_checkpoints")
    op.drop_constraint(op.f("ck_jobs_claimable_has_attempt_left"), "jobs", type_="check")
