"""GET /v1/overview, GET /v1/meta, and the display filename in GET /v1/jobs (Phase 9)."""

from datetime import timedelta

from sqlalchemy import text, update
from sqlalchemy.orm import Session

from app.api.health import get_broker_check
from app.db.models import Job
from app.db.repositories.overview import status_counts_statement
from app.domain.enums import JobStatus, WorkflowType
from app.domain.workflows import WORKFLOW_STAGES
from tests.factories import make_job, make_outbox, make_upload, utcnow
from tests.integration.conftest import TEST_MAX_PDF_BYTES, TEST_MAX_ZIP_BYTES

ZERO_COUNTS = {status.value: 0 for status in JobStatus}


def test_overview_of_an_empty_database(api):
    body = api.get("/v1/overview").json()
    assert body["counts"] == ZERO_COUNTS
    assert body["total"] == 0
    assert body["oldest_queued"] is None
    assert body["queue"] == {
        "broker": "ok",
        "pending_messages": 0,
        "scheduled_messages": 0,
        "failing_messages": 0,
        "max_dispatch_attempts": 0,
        "last_dispatch_error": None,
        "oldest_pending_seconds": None,
    }
    assert body["database_time"]


def test_overview_counts_every_status(api, engine):
    with Session(engine) as session, session.begin():
        upload = make_upload(session)
        for status, count in [
            (JobStatus.QUEUED, 2),
            (JobStatus.RUNNING, 1),
            (JobStatus.FAILED, 3),
            (JobStatus.CANCELLED, 1),
        ]:
            for _ in range(count):
                make_job(session, upload=upload, status=status)
    body = api.get("/v1/overview").json()
    assert body["counts"] == {**ZERO_COUNTS, "queued": 2, "running": 1, "failed": 3, "cancelled": 1}
    assert body["total"] == 7


def test_oldest_queued_age_uses_when_the_job_entered_queued(api, engine):
    with Session(engine) as session, session.begin():
        upload = make_upload(session)
        # Created long ago but reopened (updated) recently: its queue age is the recent part.
        reopened = make_job(session, upload=upload, created_at=utcnow() - timedelta(days=3))
        oldest = make_job(session, upload=upload)
        make_job(session, upload=upload, status=JobStatus.RUNNING)  # not queued: ignored
        oldest_id, reopened_id = oldest.id, reopened.id
    with engine.begin() as connection:
        connection.execute(
            update(Job)
            .where(Job.id == oldest_id)
            .values(updated_at=text("now() - interval '90 seconds'"))
        )
        connection.execute(
            update(Job)
            .where(Job.id == reopened_id)
            .values(updated_at=text("now() - interval '10 seconds'"))
        )
    body = api.get("/v1/overview").json()
    assert body["oldest_queued"]["job_id"] == str(oldest_id)
    assert 89 <= body["oldest_queued"]["queued_seconds"] < 120


def test_overview_reports_dispatch_failures(api, engine):
    with Session(engine) as session, session.begin():
        upload = make_upload(session)
        jobs = [make_job(session, upload=upload) for _ in range(4)]
        make_outbox(session, jobs[0])  # due, never tried
        make_outbox(session, jobs[1], dispatch_attempts=2, last_dispatch_error="ConnectionError")
        # Failing for an hour: the dispatcher's backoff moved available_at into the future.
        make_outbox(
            session,
            jobs[2],
            dispatch_attempts=5,
            last_dispatch_error="TimeoutError",
            created_at=utcnow() - timedelta(hours=1),
            available_at=utcnow() + timedelta(seconds=30),
        )
        make_outbox(session, jobs[3], available_at=utcnow() + timedelta(minutes=5))  # later
        # Published rows are history, even if publishing them once failed.
        make_outbox(
            session,
            jobs[0],
            published_at=utcnow(),
            dispatch_attempts=9,
            last_dispatch_error="OldError",
        )
    queue = api.get("/v1/overview").json()["queue"]
    assert queue["pending_messages"] == 3  # the failing row in backoff is pending, not scheduled
    assert queue["scheduled_messages"] == 1
    assert queue["failing_messages"] == 2
    assert queue["max_dispatch_attempts"] == 5
    assert queue["last_dispatch_error"] == "TimeoutError"
    assert 3590 <= queue["oldest_pending_seconds"] < 3700  # the outage's real length


def test_overview_reports_an_unreachable_broker_without_failing(api):
    def broken() -> None:
        raise ConnectionError("redis is down")

    api.app.dependency_overrides[get_broker_check] = lambda: broken
    try:
        response = api.get("/v1/overview")
    finally:
        api.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["queue"]["broker"] == "unavailable"


def test_status_counts_can_use_the_status_index(engine):
    compiled = status_counts_statement().compile(dialect=engine.dialect)
    with engine.connect() as connection:
        # On a tiny table the planner prefers a sequential scan; forbid it to see the index path.
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(connection.execute(text(f"EXPLAIN {compiled}")).scalars())
    assert "ix_jobs_status_created_at_id" in plan, plan


# --- Metadata -------------------------------------------------------------------------------------


def test_meta_reports_the_servers_limits_and_stages(api):
    body = api.get("/v1/meta").json()
    by_type = {w["workflow_type"]: w for w in body["workflows"]}
    assert set(by_type) == {w.value for w in WorkflowType}
    pdf, repo = by_type["pdf_ingest"], by_type["repo_index"]
    assert pdf["upload_kind"] == "pdf"
    assert pdf["extensions"] == [".pdf"]
    assert pdf["upload_content_type"] == "application/pdf"
    assert pdf["max_upload_bytes"] == TEST_MAX_PDF_BYTES  # the settings, not a constant
    assert bytes.fromhex(pdf["signature_hex"]) == b"%PDF-"
    assert repo["extensions"] == [".zip"]
    assert repo["max_upload_bytes"] == TEST_MAX_ZIP_BYTES
    assert "application/x-zip-compressed" in repo["accepted_content_types"]
    assert [(s["name"], s["weight"]) for s in repo["stages"]] == list(
        WORKFLOW_STAGES[WorkflowType.REPO_INDEX]
    )
    assert sum(s["weight"] for s in pdf["stages"]) == 100
    assert body["max_filename_length"] == 255
    assert body["fault_injection_enabled"] is False
    assert body["max_fault_failures"] == 20


def test_meta_reports_fault_injection_when_enabled(engine, api_settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = api_settings.model_copy(update={"enable_fault_injection": True})
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/meta").json()["fault_injection_enabled"] is True


# --- Job list display filename --------------------------------------------------------------------


def test_job_list_items_carry_the_upload_filename(api, engine):
    with Session(engine) as session, session.begin():
        report = make_upload(session, original_filename="report.pdf")
        hostile = make_upload(session, original_filename="<b>x</b> javascript:alert(1).pdf")
        make_job(session, upload=report)
        make_job(session, upload=hostile, created_at=utcnow() + timedelta(seconds=1))
        make_job(session, upload=report, created_at=utcnow() + timedelta(seconds=2))
    items = api.get("/v1/jobs").json()["items"]
    # Returned as plain JSON strings, exactly as stored; the dashboard renders them as text.
    assert [item["upload_filename"] for item in items] == [
        "report.pdf",
        "<b>x</b> javascript:alert(1).pdf",
        "report.pdf",
    ]
