"""POST /v1/jobs: idempotency, validation, and the transactional outbox."""

import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import Job, JobEvent, OutboxMessage, Upload
from app.domain.enums import EventType, UploadKind, UploadRejectionReason, UploadStatus
from tests.factories import make_upload, utcnow
from tests.samples import pdf_bytes, zip_bytes


def _key() -> str:
    return f"test-key-{uuid.uuid4()}"


def _uploaded(api, kind: str = "pdf") -> str:
    content, name, ctype = (
        (pdf_bytes(), "a.pdf", "application/pdf")
        if kind == "pdf"
        else (zip_bytes(), "repo.zip", "application/zip")
    )
    response = api.post(
        "/v1/uploads", params={"filename": name}, content=content, headers={"Content-Type": ctype}
    )
    assert response.status_code == 201
    return response.json()["id"]


def _post_job(api, body, key: str | None):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Idempotency-Key"] = key
    raw = body if isinstance(body, str) else json.dumps(body)
    return api.post("/v1/jobs", content=raw, headers=headers)


def _count(engine, model) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model))


# --- Creating a job -----------------------------------------------------------------------------


def test_new_job_returns_201_and_writes_job_event_and_outbox_together(api, engine):
    upload_id = _uploaded(api)
    response = _post_job(api, {"workflow_type": "pdf_ingest", "upload_id": upload_id}, _key())

    assert response.status_code == 201
    assert response.headers["Idempotent-Replayed"] == "false"
    job = response.json()
    assert response.headers["Location"] == f"/v1/jobs/{job['id']}"
    assert job["status"] == "queued"
    assert job["upload_id"] == upload_id
    assert job["attempt_count"] == 0 and job["max_attempts"] == 3
    assert job["options"] == {}
    assert job["last_event_seq"] == 1

    with Session(engine) as session:
        [event] = session.scalars(select(JobEvent).where(JobEvent.job_id == uuid.UUID(job["id"])))
        assert (event.seq, event.event_type) == (1, EventType.JOB_CREATED)
        assert event.payload == {"workflow_type": "pdf_ingest", "upload_id": upload_id}

        [message] = session.scalars(select(OutboxMessage))
        assert message.job_id == uuid.UUID(job["id"])
        assert message.task_name == "run_job"
        assert message.payload == {"job_id": job["id"]}
        assert message.published_at is None
        assert message.available_at <= utcnow()
        assert message.dispatch_attempts == 0


def test_job_records_the_upload_hash(api):
    upload = api.post(
        "/v1/uploads",
        params={"filename": "a.pdf"},
        content=pdf_bytes(),
        headers={"Content-Type": "application/pdf"},
    ).json()
    job = _post_job(api, {"workflow_type": "pdf_ingest", "upload_id": upload["id"]}, _key()).json()
    assert job["input_sha256"] == upload["sha256"]


def test_zip_upload_creates_a_repo_index_job(api):
    upload_id = _uploaded(api, "zip")
    response = _post_job(api, {"workflow_type": "repo_index", "upload_id": upload_id}, _key())
    assert response.status_code == 201
    assert response.json()["workflow_type"] == "repo_index"


# --- Idempotency --------------------------------------------------------------------------------


def test_replay_returns_200_with_the_same_job_and_writes_nothing_new(api, engine):
    upload_id = _uploaded(api)
    key, body = _key(), {"workflow_type": "pdf_ingest", "upload_id": upload_id}
    first = _post_job(api, body, key)
    second = _post_job(api, body, key)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert "Location" not in second.headers
    assert second.json()["id"] == first.json()["id"]
    assert _count(engine, Job) == 1
    assert _count(engine, JobEvent) == 1
    assert _count(engine, OutboxMessage) == 1


def test_replay_matches_regardless_of_key_order_whitespace_and_uuid_case(api):
    upload_id = _uploaded(api)
    key = _key()
    first = _post_job(api, {"workflow_type": "pdf_ingest", "upload_id": upload_id}, key)
    reformatted = (
        '{\n  "upload_id" :  "' + upload_id.upper() + '",\n  "workflow_type": "pdf_ingest"\n}'
    )
    second = _post_job(api, reformatted, key)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_same_key_with_a_different_request_is_409(api, engine):
    key = _key()
    first = _post_job(api, {"workflow_type": "pdf_ingest", "upload_id": _uploaded(api)}, key)
    response = _post_job(api, {"workflow_type": "pdf_ingest", "upload_id": _uploaded(api)}, key)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "idempotency_key_reused"
    assert error["details"]["job_id"] == first.json()["id"]
    assert _count(engine, Job) == 1


def test_sending_default_options_explicitly_differs_from_omitting_them(api):
    """Documented: the hash covers only the fields the client sent."""
    upload_id, key = _uploaded(api), _key()
    assert (
        _post_job(api, {"workflow_type": "pdf_ingest", "upload_id": upload_id}, key).status_code
        == 201
    )
    response = _post_job(
        api, {"workflow_type": "pdf_ingest", "upload_id": upload_id, "options": {}}, key
    )
    assert response.status_code == 409


def test_replay_still_works_after_the_upload_expired(api, engine):
    upload_id = _uploaded(api)
    key, body = _key(), {"workflow_type": "pdf_ingest", "upload_id": upload_id}
    first = _post_job(api, body, key)
    with Session(engine) as session, session.begin():
        session.execute(
            update(Upload)
            .where(Upload.id == uuid.UUID(upload_id))
            .values(
                created_at=utcnow() - timedelta(days=3), expires_at=utcnow() - timedelta(days=1)
            )
        )
    replay = _post_job(api, body, key)
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]


def test_a_rejected_request_does_not_use_up_the_key(api):
    key = _key()
    bad = _post_job(api, {"workflow_type": "pdf_ingest", "upload_id": str(uuid.uuid4())}, key)
    assert bad.status_code == 422
    good = _post_job(api, {"workflow_type": "pdf_ingest", "upload_id": _uploaded(api)}, key)
    assert good.status_code == 201


def test_one_upload_can_be_used_by_several_jobs(api, engine):
    upload_id = _uploaded(api)
    body = {"workflow_type": "pdf_ingest", "upload_id": upload_id}
    first = _post_job(api, body, _key())
    second = _post_job(api, body, _key())
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert _count(engine, Job) == 2


# --- Idempotency-Key validation -------------------------------------------------------------------


def test_missing_key_is_400(api, engine):
    response = _post_job(api, {"workflow_type": "pdf_ingest", "upload_id": _uploaded(api)}, None)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "idempotency_key_missing"
    assert "format" in error["details"]
    assert _count(engine, Job) == 0


@pytest.mark.parametrize(
    "key",
    [
        "a" * 15,
        "a" * 129,
        "spaces are not allowed!",
        'quote"in-the-key-00',
        "é".encode("latin-1") * 20,  # raw non-ASCII header bytes, as a client could send them
    ],
)
def test_malformed_key_is_400(api, engine, key):
    upload_id = _uploaded(api)
    headers = {"Content-Type": "application/json", "Idempotency-Key": key}
    body = json.dumps({"workflow_type": "pdf_ingest", "upload_id": upload_id})
    response = api.post("/v1/jobs", content=body, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_invalid"
    assert _count(engine, Job) == 0


@pytest.mark.parametrize("key", ["a" * 16, "b" * 128, str(uuid.uuid4())])
def test_boundary_keys_are_accepted(api, key):
    body = {"workflow_type": "pdf_ingest", "upload_id": _uploaded(api)}
    assert _post_job(api, body, key).status_code == 201


# --- Upload validation ----------------------------------------------------------------------------


def test_unknown_upload_is_422(api):
    response = _post_job(
        api, {"workflow_type": "pdf_ingest", "upload_id": str(uuid.uuid4())}, _key()
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "upload_not_found"


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": UploadStatus.RECEIVING, "sha256": None, "size_bytes": None},
        {"status": UploadStatus.REJECTED, "rejection_reason": UploadRejectionReason.TOO_LARGE},
        {"status": UploadStatus.DELETED, "deleted_at": utcnow()},
    ],
    ids=["receiving", "rejected", "deleted"],
)
def test_upload_that_is_not_ready_is_422(api, engine, overrides):
    with Session(engine) as session, session.begin():
        upload_id = str(make_upload(session, **overrides).id)
    response = _post_job(api, {"workflow_type": "pdf_ingest", "upload_id": upload_id}, _key())
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "upload_not_ready"
    assert error["details"]["upload_status"] == overrides["status"]


def test_expired_upload_is_422(api, engine):
    with Session(engine) as session, session.begin():
        upload_id = str(
            make_upload(
                session,
                created_at=utcnow() - timedelta(days=2),
                expires_at=utcnow() - timedelta(seconds=1),
            ).id
        )
    response = _post_job(api, {"workflow_type": "pdf_ingest", "upload_id": upload_id}, _key())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "upload_expired"


@pytest.mark.parametrize(
    ("workflow_type", "upload_kind"),
    [("pdf_ingest", UploadKind.ZIP), ("repo_index", UploadKind.PDF)],
)
def test_upload_kind_must_match_the_workflow(api, engine, workflow_type, upload_kind):
    with Session(engine) as session, session.begin():
        upload_id = str(make_upload(session, kind=upload_kind).id)
    response = _post_job(api, {"workflow_type": workflow_type, "upload_id": upload_id}, _key())
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "upload_kind_mismatch"
    assert error["details"]["upload_kind"] == upload_kind


# --- Request body validation ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"workflow_type": "video_transcode", "upload_id": "x"}, "workflow_type"),
        ({"workflow_type": "pdf_ingest", "upload_id": "not-a-uuid"}, "upload_id"),
        ({"workflow_type": "pdf_ingest"}, "upload_id"),
        ({"workflow_type": "pdf_ingest", "upload_id": str(uuid.uuid4()), "extra": 1}, "extra"),
        (
            {"workflow_type": "pdf_ingest", "upload_id": str(uuid.uuid4()), "options": {"x": 1}},
            "options.x",
        ),
    ],
)
def test_invalid_body_is_422_with_the_field_named(api, body, field):
    response = _post_job(api, body, _key())
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert field in [f["field"] for f in error["details"]["fields"]]


def test_malformed_json_is_422(api):
    response = _post_job(api, '{"workflow_type": ', _key())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_oversized_json_body_is_413(api):
    huge = json.dumps({"workflow_type": "pdf_ingest", "upload_id": "x", "pad": "x" * 70_000})
    response = _post_job(api, huge, _key())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


# --- Atomicity ------------------------------------------------------------------------------------


def test_a_failure_writing_the_outbox_leaves_no_job_or_event(api, engine, monkeypatch):
    def broken_enqueue(*args, **kwargs):
        raise RuntimeError("outbox insert failed")

    monkeypatch.setattr("app.services.jobs.enqueue_run_job", broken_enqueue)
    response = _post_job(api, {"workflow_type": "pdf_ingest", "upload_id": _uploaded(api)}, _key())

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert _count(engine, Job) == 0
    assert _count(engine, JobEvent) == 0
    assert _count(engine, OutboxMessage) == 0


# --- Review follow-ups --------------------------------------------------------------------------


def test_oversized_json_body_without_content_length_is_413(api):
    """The JSON limit is enforced on bytes received, not only on the Content-Length header."""
    from tests.asgi import post_chunks

    chunks = [b'{"workflow_type": "pdf_ingest", "pad": "'] + [b"x" * 8192] * 10 + [b'"}']
    result = post_chunks(
        api.app,
        "/v1/jobs",
        headers={"Content-Type": "application/json", "Idempotency-Key": _key()},
        chunks=chunks,
    )
    assert result.status == 413
    assert result.body["error"]["code"] == "request_too_large"
    assert result.chunks_read < len(chunks)  # stopped reading early


def test_body_that_is_not_utf8_is_reported_as_invalid_json(api):
    response = api.post(
        "/v1/jobs",
        content=b"\xff\xfe\x00{",
        headers={"Content-Type": "application/json", "Idempotency-Key": _key()},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["details"]["fields"][0]["issue"] == "Invalid JSON."


def test_invalid_uuid_message_does_not_quote_the_input(api):
    response = _post_job(api, {"workflow_type": "pdf_ingest", "upload_id": "SECRETVALUE"}, _key())
    assert response.status_code == 422
    assert "SECRETVALUE" not in response.text
    [field] = response.json()["error"]["details"]["fields"]
    # Pydantic's own message would say "...found `S` at 1"; it is replaced with fixed text.
    assert field == {
        "location": "body",
        "field": "upload_id",
        "issue": "Input should be a valid UUID.",
    }
