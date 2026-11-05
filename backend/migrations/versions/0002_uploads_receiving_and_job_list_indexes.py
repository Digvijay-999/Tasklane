"""Uploads can be 'receiving' without size/hash; rejection reasons; job list indexes.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-03

- uploads.sha256 and uploads.size_bytes become nullable: they are unknown until a streamed
  upload finishes. A ready upload must have both (ck_uploads_ready_has_content).
- uploads.rejection_reason records why an upload was rejected.
- Three indexes for GET /v1/jobs keyset pagination (newest first, id as tie-breaker).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("uploads", "sha256", existing_type=sa.Text(), nullable=True)
    op.alter_column("uploads", "size_bytes", existing_type=sa.BigInteger(), nullable=True)
    op.add_column("uploads", sa.Column("rejection_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_uploads_ready_has_content"),
        "uploads",
        "status <> 'ready' OR (sha256 IS NOT NULL AND size_bytes IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_uploads_rejected_has_reason"),
        "uploads",
        "status <> 'rejected' OR rejection_reason IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_uploads_reason_only_when_rejected"),
        "uploads",
        "status IN ('deleted', 'rejected') OR rejection_reason IS NULL",
    )
    op.create_check_constraint(
        op.f("ck_uploads_rejection_reason"),
        "uploads",
        "rejection_reason IN ('client_disconnected', 'empty_file', 'internal_error', "
        "'interrupted', 'signature_mismatch', 'too_large')",
    )

    op.create_index(
        op.f("ix_jobs_created_at_id"),
        "jobs",
        [sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        op.f("ix_jobs_status_created_at_id"),
        "jobs",
        ["status", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        op.f("ix_jobs_workflow_type_created_at_id"),
        "jobs",
        ["workflow_type", sa.text("created_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_jobs_workflow_type_created_at_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_status_created_at_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_created_at_id"), table_name="jobs")

    op.drop_constraint(op.f("ck_uploads_rejection_reason"), "uploads", type_="check")
    op.drop_constraint(op.f("ck_uploads_reason_only_when_rejected"), "uploads", type_="check")
    op.drop_constraint(op.f("ck_uploads_rejected_has_reason"), "uploads", type_="check")
    op.drop_constraint(op.f("ck_uploads_ready_has_content"), "uploads", type_="check")
    op.drop_column("uploads", "rejection_reason")

    # Uploads without a size or hash were never completed (receiving, or rejected while
    # streaming). No job can reference them (jobs require 'ready'), and 0001 can't store them, so
    # they are removed before restoring NOT NULL. Their files, if any, are left on disk.
    op.execute("DELETE FROM uploads WHERE sha256 IS NULL OR size_bytes IS NULL")
    op.alter_column("uploads", "size_bytes", existing_type=sa.BigInteger(), nullable=False)
    op.alter_column("uploads", "sha256", existing_type=sa.Text(), nullable=False)
