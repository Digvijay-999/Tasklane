"""Initial schema: uploads, jobs, job_attempts, job_events, artifacts, outbox.

Revision ID: 0001
Revises:
Create Date: 2026-11-02

This migration is a frozen snapshot: it spells out every value list instead of importing the
app's enums, so changing app code later can never silently change what this migration does.
Changes to the schema go in new migrations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SHA256 = "'^[0-9a-f]{64}$'"
TIMESTAMPTZ = sa.DateTime(timezone=True)
EMPTY_JSON = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "uploads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="receiving", nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
        sa.Column("deleted_at", TIMESTAMPTZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_uploads")),
        sa.UniqueConstraint("storage_path", name=op.f("uq_uploads_storage_path")),
        sa.CheckConstraint("kind IN ('pdf', 'zip')", name=op.f("ck_uploads_kind")),
        sa.CheckConstraint(
            "status IN ('deleted', 'ready', 'receiving', 'rejected')",
            name=op.f("ck_uploads_status"),
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_uploads_size_positive")),
        sa.CheckConstraint(f"sha256 ~ {SHA256}", name=op.f("ck_uploads_sha256_format")),
        sa.CheckConstraint(
            "char_length(original_filename) BETWEEN 1 AND 255",
            name=op.f("ck_uploads_filename_length"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_uploads_expiry_after_creation")
        ),
        sa.CheckConstraint(
            "(status = 'deleted') = (deleted_at IS NOT NULL)",
            name=op.f("ck_uploads_deleted_consistent"),
        ),
    )
    op.create_index(
        op.f("ix_uploads_expires_at_pending"),
        "uploads",
        ["expires_at"],
        postgresql_where=sa.text("status <> 'deleted'"),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("current_stage", sa.Text(), nullable=True),
        sa.Column("progress_percent", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column("input_sha256", sa.Text(), nullable=False),
        sa.Column("options", postgresql.JSONB(), server_default=EMPTY_JSON, nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.SmallInteger(), server_default="3", nullable=False),
        sa.Column("manual_retry_count", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("retry_at", TIMESTAMPTZ, nullable=True),
        sa.Column("cancel_requested_at", TIMESTAMPTZ, nullable=True),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("last_event_seq", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", TIMESTAMPTZ, nullable=True),
        sa.Column("finished_at", TIMESTAMPTZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.ForeignKeyConstraint(
            ["upload_id"],
            ["uploads.id"],
            name=op.f("fk_jobs_upload_id_uploads"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_jobs_idempotency_key")),
        sa.CheckConstraint(
            "status IN ('cancel_requested', 'cancelled', 'failed', 'queued', 'retry_wait', "
            "'running', 'succeeded')",
            name=op.f("ck_jobs_status"),
        ),
        sa.CheckConstraint(
            "workflow_type IN ('pdf_ingest', 'repo_index')", name=op.f("ck_jobs_workflow_type")
        ),
        sa.CheckConstraint(
            "progress_percent BETWEEN 0 AND 100", name=op.f("ck_jobs_progress_range")
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 20 AND attempt_count BETWEEN 0 AND max_attempts",
            name=op.f("ck_jobs_attempt_budget"),
        ),
        sa.CheckConstraint(
            "manual_retry_count >= 0", name=op.f("ck_jobs_manual_retry_count_nonneg")
        ),
        sa.CheckConstraint(
            "(status IN ('cancelled', 'failed', 'succeeded')) = (finished_at IS NOT NULL)",
            name=op.f("ck_jobs_finished_iff_terminal"),
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR coalesce(char_length(error_summary), 0) > 0",
            name=op.f("ck_jobs_failed_has_error"),
        ),
        sa.CheckConstraint(
            "status <> 'retry_wait' OR retry_at IS NOT NULL",
            name=op.f("ck_jobs_retry_wait_has_retry_at"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('cancel_requested', 'cancelled') OR cancel_requested_at IS NOT NULL",
            name=op.f("ck_jobs_cancel_has_timestamp"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('queued', 'retry_wait', 'running') OR cancel_requested_at IS NULL",
            name=op.f("ck_jobs_no_cancel_while_active"),
        ),
        sa.CheckConstraint(f"input_sha256 ~ {SHA256}", name=op.f("ck_jobs_input_sha256_format")),
        sa.CheckConstraint(f"request_hash ~ {SHA256}", name=op.f("ck_jobs_request_hash_format")),
        sa.CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 255", name=op.f("ck_jobs_key_length")
        ),
        sa.CheckConstraint("last_event_seq >= 0", name=op.f("ck_jobs_last_event_seq_nonneg")),
        sa.CheckConstraint("jsonb_typeof(options) = 'object'", name=op.f("ck_jobs_options_object")),
    )
    op.create_index(op.f("ix_jobs_upload_id"), "jobs", ["upload_id"])

    op.create_table(
        "job_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.SmallInteger(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("started_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("heartbeat_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("lease_expires_at", TIMESTAMPTZ, nullable=False),
        sa.Column("ended_at", TIMESTAMPTZ, nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_attempts")),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_job_attempts_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.UniqueConstraint("job_id", "attempt_no", name=op.f("uq_job_attempts_job_id_attempt_no")),
        sa.CheckConstraint("attempt_no >= 1", name=op.f("ck_job_attempts_attempt_no_positive")),
        sa.CheckConstraint(
            "trigger IN ('auto_retry', 'initial', 'manual_retry', 'recovery')",
            name=op.f("ck_job_attempts_trigger"),
        ),
        sa.CheckConstraint(
            "outcome IN ('cancelled', 'failed_permanent', 'failed_transient', 'lease_expired', "
            "'succeeded')",
            name=op.f("ck_job_attempts_outcome"),
        ),
        sa.CheckConstraint(
            "(ended_at IS NULL) = (outcome IS NULL)", name=op.f("ck_job_attempts_ended_iff_outcome")
        ),
        sa.CheckConstraint(
            "lease_expires_at > started_at", name=op.f("ck_job_attempts_lease_after_start")
        ),
        sa.CheckConstraint(
            "heartbeat_at >= started_at", name=op.f("ck_job_attempts_heartbeat_after_start")
        ),
    )
    op.create_index(
        op.f("uq_job_attempts_one_open_per_job"),
        "job_attempts",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        op.f("ix_job_attempts_open_lease_expires_at"),
        "job_attempts",
        ["lease_expires_at"],
        postgresql_where=sa.text("ended_at IS NULL"),
    )

    op.create_table(
        "job_events",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("attempt_no", sa.SmallInteger(), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), server_default=EMPTY_JSON, nullable=False),
        sa.PrimaryKeyConstraint("job_id", "seq", name=op.f("pk_job_events")),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_job_events_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_job_events_seq_positive")),
        sa.CheckConstraint(
            "event_type ~ '^[a-z_]+\\.[a-z_]+$'", name=op.f("ck_job_events_event_type_format")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name=op.f("ck_job_events_payload_object")
        ),
    )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.SmallInteger(), nullable=False),
        sa.Column("artifact_type", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifacts")),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_artifacts_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "job_id", "artifact_type", name=op.f("uq_artifacts_job_id_artifact_type")
        ),
        sa.UniqueConstraint("storage_path", name=op.f("uq_artifacts_storage_path")),
        sa.CheckConstraint(
            "artifact_type IN ('chunks', 'manifest', 'summary')",
            name=op.f("ck_artifacts_artifact_type"),
        ),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_artifacts_size_nonneg")),
        sa.CheckConstraint(f"sha256 ~ {SHA256}", name=op.f("ck_artifacts_sha256_format")),
        sa.CheckConstraint("attempt_no >= 1", name=op.f("ck_artifacts_attempt_no_positive")),
    )

    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("task_name", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("available_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("published_at", TIMESTAMPTZ, nullable=True),
        sa.Column("dispatch_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_dispatch_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_outbox_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.CheckConstraint("task_name IN ('run_job')", name=op.f("ck_outbox_task_name")),
        sa.CheckConstraint(
            "dispatch_attempts >= 0", name=op.f("ck_outbox_dispatch_attempts_nonneg")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name=op.f("ck_outbox_payload_object")
        ),
    )
    op.create_index(
        op.f("uq_outbox_one_pending_per_job"),
        "outbox",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_index(
        op.f("ix_outbox_pending_available_at"),
        "outbox",
        ["available_at", "id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_index(op.f("ix_outbox_job_id"), "outbox", ["job_id"])


def downgrade() -> None:
    # Reverse order of creation, so foreign keys never point at a dropped table.
    # Dropping a table also drops its indexes and constraints.
    op.drop_table("outbox")
    op.drop_table("artifacts")
    op.drop_table("job_events")
    op.drop_table("job_attempts")
    op.drop_table("jobs")
    op.drop_table("uploads")
