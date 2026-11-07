"""Provider call records.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06

One row per call to a summarization provider (successful or not): which provider and model,
latency, characters in and out, and token usage when the provider reports it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "provider_calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.SmallInteger(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("chunk_id", sa.Text(), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("started_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("input_chars", sa.Integer(), nullable=False),
        sa.Column("output_chars", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_calls")),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_provider_calls_job_id_jobs"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("attempt_no >= 1", name=op.f("ck_provider_calls_attempt_no_positive")),
        sa.CheckConstraint(
            "purpose IN ('chunk', 'combine')", name=op.f("ck_provider_calls_purpose")
        ),
        sa.CheckConstraint("latency_ms >= 0", name=op.f("ck_provider_calls_latency_nonneg")),
        sa.CheckConstraint(
            "input_chars >= 0 AND (output_chars IS NULL OR output_chars >= 0)",
            name=op.f("ck_provider_calls_chars_nonneg"),
        ),
        sa.CheckConstraint(
            "coalesce(prompt_tokens, 0) >= 0 AND coalesce(completion_tokens, 0) >= 0 "
            "AND coalesce(total_tokens, 0) >= 0",
            name=op.f("ck_provider_calls_tokens_nonneg"),
        ),
        sa.CheckConstraint(
            "success = (error_class IS NULL)", name=op.f("ck_provider_calls_success_iff_no_error")
        ),
    )
    op.create_index(op.f("ix_provider_calls_job_id"), "provider_calls", ["job_id"])


def downgrade() -> None:
    op.drop_table("provider_calls")
