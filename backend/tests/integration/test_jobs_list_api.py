"""GET /v1/jobs (keyset pagination, filters) and GET /v1/jobs/{id}."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.repositories.jobs import list_jobs_statement
from app.domain.enums import AttemptOutcome, JobStatus, WorkflowType
from app.domain.listing import JobListPosition
from tests.factories import make_artifact, make_attempt, make_job, make_upload, utcnow

BASE_TIME = utcnow() - timedelta(days=1)


def _make_jobs(engine, count: int, *, start: int = 0, **overrides) -> list[str]:
    """Insert `count` jobs one second apart (later index = newer). Returns ids oldest first."""
    ids = []
    with Session(engine) as session, session.begin():
        upload = make_upload(session)
        for i in range(start, start + count):
            job = make_job(
                session, upload=upload, created_at=BASE_TIME + timedelta(seconds=i), **overrides
            )
            ids.append(str(job.id))
    return ids


def _all_pages(api, params: dict) -> list[list[str]]:
    pages, cursor = [], None
    while True:
        query = {**params, **({"cursor": cursor} if cursor else {})}
        body = api.get("/v1/jobs", params=query).json()
        pages.append([item["id"] for item in body["items"]])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages


# --- Ordering and paging --------------------------------------------------------------------------


def test_lists_newest_first(api, engine):
    ids = _make_jobs(engine, 3)
    body = api.get("/v1/jobs").json()
    assert [item["id"] for item in body["items"]] == list(reversed(ids))
    assert body["next_cursor"] is None


def test_empty_list(api):
    assert api.get("/v1/jobs").json() == {"items": [], "next_cursor": None}


def test_pages_cover_every_job_exactly_once(api, engine):
    ids = _make_jobs(engine, 7)
    pages = _all_pages(api, {"limit": 3})
    assert [len(page) for page in pages] == [3, 3, 1]
    assert [job_id for page in pages for job_id in page] == list(reversed(ids))


def test_exact_multiple_of_page_size_has_no_empty_last_page(api, engine):
    _make_jobs(engine, 4)
    assert [len(page) for page in _all_pages(api, {"limit": 2})] == [2, 2]


def test_pagination_is_stable_when_new_jobs_arrive_between_pages(api, engine):
    original = _make_jobs(engine, 5)  # oldest first
    first = api.get("/v1/jobs", params={"limit": 2}).json()

    # Three newer jobs arrive after page 1 was fetched (with page numbers, these would push
    # rows down and cause duplicates on page 2).
    _make_jobs(engine, 3, start=100)

    rest, cursor = [], first["next_cursor"]
    while cursor:
        body = api.get("/v1/jobs", params={"limit": 2, "cursor": cursor}).json()
        rest += [item["id"] for item in body["items"]]
        cursor = body["next_cursor"]

    seen = [item["id"] for item in first["items"]] + rest
    assert seen == list(reversed(original))  # every original job once, in order, no new ones


def test_jobs_with_identical_timestamps_are_paged_without_gaps_or_duplicates(api, engine):
    same_time = utcnow() - timedelta(hours=1)
    with Session(engine) as session, session.begin():
        upload = make_upload(session)
        ids = {str(make_job(session, upload=upload, created_at=same_time).id) for _ in range(5)}

    pages = _all_pages(api, {"limit": 2})
    flat = [job_id for page in pages for job_id in page]
    assert len(flat) == 5 and set(flat) == ids
    assert flat == sorted(flat, key=uuid.UUID, reverse=True)  # ties broken by id, descending


# --- Filters -------------------------------------------------------------------------------------


def test_filter_by_status(api, engine):
    queued = _make_jobs(engine, 2)
    failed = _make_jobs(engine, 2, start=10, status=JobStatus.FAILED)
    _make_jobs(engine, 1, start=20, status=JobStatus.RUNNING)

    def listed(params):
        return {item["id"] for item in api.get("/v1/jobs", params=params).json()["items"]}

    assert listed({"status": "queued"}) == set(queued)
    assert listed({"status": ["queued", "failed"]}) == set(queued) | set(failed)


def test_filter_by_workflow_type_and_combined(api, engine):
    pdf = _make_jobs(engine, 2)
    with Session(engine) as session, session.begin():
        upload = make_upload(session, kind="zip")
        zip_queued = str(make_job(session, upload=upload, workflow_type="repo_index").id)
        make_job(session, upload=upload, workflow_type="repo_index", status=JobStatus.FAILED)

    def listed(params):
        return {item["id"] for item in api.get("/v1/jobs", params=params).json()["items"]}

    assert listed({"workflow_type": "pdf_ingest"}) == set(pdf)
    assert listed({"workflow_type": "repo_index", "status": "queued"}) == {zip_queued}


def test_filters_apply_across_pages(api, engine):
    queued = _make_jobs(engine, 5)
    _make_jobs(engine, 5, start=50, status=JobStatus.FAILED)
    pages = _all_pages(api, {"status": "queued", "limit": 2})
    assert [job_id for page in pages for job_id in page] == list(reversed(queued))


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"status": "paused"}, {"workflow_type": "video"}],
)
def test_invalid_query_parameters_are_422(api, params):
    response = api.get("/v1/jobs", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_default_page_size_is_20_and_max_is_100(api, engine):
    _make_jobs(engine, 25)
    assert len(api.get("/v1/jobs").json()["items"]) == 20
    assert len(api.get("/v1/jobs", params={"limit": 100}).json()["items"]) == 25


def test_garbage_cursor_is_400(api):
    response = api.get("/v1/jobs", params={"cursor": "definitely-not-a-cursor"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"


def test_cursor_reused_with_other_filters_is_400(api, engine):
    _make_jobs(engine, 3)
    cursor = api.get("/v1/jobs", params={"limit": 1}).json()["next_cursor"]
    response = api.get("/v1/jobs", params={"limit": 1, "status": "queued", "cursor": cursor})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"


# --- Index usage ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filters", "index"),
    [
        ({}, "ix_jobs_created_at_id"),
        ({"statuses": ["queued"]}, "ix_jobs_status_created_at_id"),
        ({"workflow_types": ["pdf_ingest"]}, "ix_jobs_workflow_type_created_at_id"),
    ],
)
@pytest.mark.parametrize("with_cursor", [False, True])
def test_list_queries_can_use_their_index(engine, filters, index, with_cursor):
    """With sequential scans disabled, Postgres must find a plan using the matching index.
    (On a tiny table it would otherwise just read the whole table, which proves nothing.)"""
    after = JobListPosition(utcnow(), uuid.uuid4()) if with_cursor else None
    statement = list_jobs_statement(**filters, after=after, limit=21)
    compiled = statement.compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True})
    with engine.connect() as connection:
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(connection.execute(text(f"EXPLAIN {compiled}")).scalars())
    assert index in plan, plan
    assert "Sort" not in plan, plan  # rows come out of the index already in order


# --- Detail ---------------------------------------------------------------------------------------


def test_detail_includes_upload_attempts_and_artifacts(api, engine):
    with Session(engine) as session, session.begin():
        upload = make_upload(session, original_filename="report.pdf")
        job = make_job(session, upload=upload, status=JobStatus.RUNNING, attempt_count=2)
        make_attempt(
            session,
            job,
            attempt_no=1,
            ended_at=utcnow(),
            outcome=AttemptOutcome.FAILED_TRANSIENT,
            error_class="provider_timeout",
            error_summary="The summarizer timed out.",
        )
        make_attempt(session, job, attempt_no=2, worker_id="worker-b")
        make_artifact(session, job, artifact_type="summary")
        make_artifact(session, job, artifact_type="manifest")
        job_id, upload_id = str(job.id), str(upload.id)

    response = api.get(f"/v1/jobs/{job_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == job_id and body["status"] == "running"
    assert body["upload"]["id"] == upload_id
    assert body["upload"]["original_filename"] == "report.pdf"
    assert [a["attempt_no"] for a in body["attempts"]] == [1, 2]
    assert body["attempts"][0]["outcome"] == "failed_transient"
    assert body["attempts"][0]["error_summary"] == "The summarizer timed out."
    assert body["attempts"][1]["worker_id"] == "worker-b"
    assert body["attempts"][1]["ended_at"] is None
    assert [a["artifact_type"] for a in body["artifacts"]] == ["manifest", "summary"]
    assert "storage_path" not in body["artifacts"][0]  # internal paths are not exposed


def test_detail_of_a_new_job_has_empty_attempts_and_artifacts(api, engine):
    [job_id] = _make_jobs(engine, 1)
    body = api.get(f"/v1/jobs/{job_id}").json()
    assert body["attempts"] == [] and body["artifacts"] == []
    assert body["workflow_type"] == WorkflowType.PDF_INGEST


@pytest.mark.parametrize("job_id", [str(uuid.uuid4()), "not-a-uuid", "123"])
def test_unknown_or_malformed_job_id_is_404(api, job_id):
    response = api.get(f"/v1/jobs/{job_id}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"
