"""GET /v1/jobs/{job_id}/artifacts/{artifact_id}."""

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.repositories.artifacts import upsert_artifact
from app.storage.local import LocalStorage
from tests.factories import make_job


@pytest.fixture
def stored_artifact(engine, api_settings):
    """A job with a real summary file on disk and its artifact row."""
    content = b"# Summary of report.pdf\n\nHello.\n"
    with Session(engine) as session, session.begin():
        job = make_job(session)
        relative = f"artifacts/{job.id}/summary.md"
        path = LocalStorage(api_settings.storage_root).absolute(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        upsert_artifact(
            session,
            job_id=job.id,
            attempt_no=1,
            artifact_type="summary",
            storage_path=relative,
            filename="summary.md",
            content_type="text/markdown; charset=utf-8",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        job_id = job.id
    return job_id, path, content


def _artifact_id(api, job_id) -> str:
    [artifact] = api.get(f"/v1/jobs/{job_id}").json()["artifacts"]
    return artifact["id"]


def test_download_returns_the_file_with_safe_headers(api, stored_artifact):
    job_id, _, content = stored_artifact
    detail = api.get(f"/v1/jobs/{job_id}").json()
    [artifact] = detail["artifacts"]
    assert artifact["download_url"] == f"/v1/jobs/{job_id}/artifacts/{artifact['id']}"

    response = api.get(artifact["download_url"])

    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["content-disposition"] == 'attachment; filename="summary.md"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["etag"] == f'"{hashlib.sha256(content).hexdigest()}"'


def test_upserting_the_same_artifact_type_replaces_the_row(api, engine, stored_artifact):
    job_id, path, _ = stored_artifact
    first_id = _artifact_id(api, job_id)
    new_content = b"# Summary\n\nRegenerated.\n"
    path.write_bytes(new_content)
    with Session(engine) as session, session.begin():
        upsert_artifact(
            session,
            job_id=job_id,
            attempt_no=2,
            artifact_type="summary",
            storage_path=f"artifacts/{job_id}/summary.md",
            filename="summary.md",
            content_type="text/markdown; charset=utf-8",
            size_bytes=len(new_content),
            sha256=hashlib.sha256(new_content).hexdigest(),
        )
    [artifact] = api.get(f"/v1/jobs/{job_id}").json()["artifacts"]  # still exactly one
    assert artifact["id"] == first_id and artifact["attempt_no"] == 2
    assert api.get(artifact["download_url"]).content == new_content


def test_artifact_of_another_job_is_not_found(api, engine, stored_artifact):
    job_id, _, _ = stored_artifact
    with Session(engine) as session, session.begin():
        other_job_id = make_job(session).id
    response = api.get(f"/v1/jobs/{other_job_id}/artifacts/{_artifact_id(api, job_id)}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "artifact_not_found"


@pytest.mark.parametrize(
    "path", ["/v1/jobs/{job}/artifacts/{random}", "/v1/jobs/{job}/artifacts/not-a-uuid"]
)
def test_unknown_artifact_is_not_found(api, stored_artifact, path):
    job_id, _, _ = stored_artifact
    response = api.get(path.format(job=job_id, random=uuid.uuid4()))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "artifact_not_found"


@pytest.mark.parametrize("damage", ["deleted", "truncated"])
def test_missing_or_changed_file_is_gone(api, stored_artifact, damage):
    job_id, path, _ = stored_artifact
    if damage == "deleted":
        path.unlink()
    else:
        path.write_bytes(b"x")
    response = api.get(f"/v1/jobs/{job_id}/artifacts/{_artifact_id(api, job_id)}")
    assert response.status_code == 410
    error = response.json()["error"]
    assert error["code"] == "artifact_gone"
    assert str(path) not in error["message"]  # no internal paths in messages


def test_a_stored_path_outside_the_storage_root_is_never_served(api, engine, api_settings):
    """Paths come from the database, not the request; even a corrupted row can't escape the
    storage root, and the error says nothing about paths."""
    secret = api_settings.storage_root.parent / "outside.txt"
    secret.write_text("not for download")
    with Session(engine) as session, session.begin():
        job = make_job(session)
        upsert_artifact(
            session,
            job_id=job.id,
            attempt_no=1,
            artifact_type="summary",
            storage_path="../outside.txt",
            filename="summary.md",
            content_type="text/markdown; charset=utf-8",
            size_bytes=len("not for download"),
            sha256="0" * 64,
        )
        job_id = job.id
    with TestClient(api.app, raise_server_exceptions=False) as client:
        response = client.get(f"/v1/jobs/{job_id}/artifacts/{_artifact_id(api, job_id)}")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "outside" not in response.text and "not for download" not in response.text
