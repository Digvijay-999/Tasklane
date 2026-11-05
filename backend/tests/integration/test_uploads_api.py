"""POST /v1/uploads against the real database and a temporary storage folder."""

import hashlib
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Upload
from app.domain.enums import UploadRejectionReason, UploadStatus
from app.storage.local import LocalStorage
from tests.asgi import post_chunks
from tests.integration.conftest import TEST_MAX_PDF_BYTES, TEST_MAX_ZIP_BYTES
from tests.samples import pdf_bytes, zip_bytes

PDF_HEADERS = {"Content-Type": "application/pdf"}
ZIP_HEADERS = {"Content-Type": "application/zip"}


def _upload(api, content, filename="sample.pdf", headers=PDF_HEADERS):
    return api.post("/v1/uploads", params={"filename": filename}, content=content, headers=headers)


def _rows(engine) -> list[Upload]:
    with Session(engine) as session:
        return list(session.scalars(select(Upload)))


def _stored_files(storage: LocalStorage) -> list[str]:
    if not storage.root.exists():
        return []
    return sorted(
        str(path.relative_to(storage.root)).replace("\\", "/")
        for path in storage.root.rglob("*")
        if path.is_file()
    )


def _assert_rejected(engine, storage, reason: UploadRejectionReason) -> None:
    [row] = _rows(engine)
    assert row.status == UploadStatus.REJECTED
    assert row.rejection_reason == reason
    assert _stored_files(storage) == []  # temporary and final files both removed


# --- Accepted uploads ---------------------------------------------------------------------------


def test_pdf_upload_is_stored_under_a_generated_path(api, engine, storage):
    content = pdf_bytes()
    response = _upload(api, content)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "ready"
    assert body["kind"] == "pdf"
    assert body["content_type"] == "application/pdf"
    assert body["size_bytes"] == len(content)
    assert body["sha256"] == hashlib.sha256(content).hexdigest()
    assert body["original_filename"] == "sample.pdf"

    [row] = _rows(engine)
    assert row.storage_path == f"uploads/{body['id']}/source.pdf"
    assert storage.absolute(row.storage_path).read_bytes() == content
    assert _stored_files(storage) == [row.storage_path]  # no leftover .part file


@pytest.mark.parametrize(
    "content_type", ["application/zip", "application/x-zip-compressed", "application/octet-stream"]
)
def test_zip_upload_accepts_common_declared_types(api, engine, content_type):
    response = _upload(api, zip_bytes(), "repo.zip", {"Content-Type": content_type})
    assert response.status_code == 201
    # The stored type is the one proven by the signature, not the declared one.
    assert response.json()["content_type"] == "application/zip"


def test_extension_is_case_insensitive(api):
    assert _upload(api, pdf_bytes(), "REPORT.PDF").status_code == 201


def test_filename_is_display_only_never_a_path(api, engine, storage):
    response = _upload(api, pdf_bytes(), "../../outside/evil.pdf")
    assert response.status_code == 201
    [row] = _rows(engine)
    assert row.original_filename == "../../outside/evil.pdf"
    assert "evil" not in row.storage_path and ".." not in row.storage_path
    assert _stored_files(storage) == [row.storage_path]


def test_non_ascii_filename_round_trips(api):
    response = _upload(api, pdf_bytes(), "résumé 2026.pdf")
    assert response.status_code == 201
    assert response.json()["original_filename"] == "résumé 2026.pdf"


def test_expiry_is_set_from_the_configured_lifetime(api):
    from datetime import datetime, timedelta

    body = _upload(api, pdf_bytes()).json()
    lifetime = datetime.fromisoformat(body["expires_at"]) - datetime.fromisoformat(
        body["created_at"]
    )
    assert lifetime == timedelta(hours=24)


# --- Size limits ----------------------------------------------------------------------------------


def test_file_exactly_at_the_limit_is_accepted(api):
    assert _upload(api, pdf_bytes(TEST_MAX_PDF_BYTES)).status_code == 201


def test_oversized_content_length_is_rejected_before_reading(api, engine, storage):
    response = _upload(api, pdf_bytes(TEST_MAX_PDF_BYTES + 1))
    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "upload_too_large"
    assert error["details"]["limit_bytes"] == TEST_MAX_PDF_BYTES
    assert _rows(engine) == []  # failed fast: nothing recorded, nothing written
    assert _stored_files(storage) == []


def test_oversized_body_without_content_length_is_rejected_while_streaming(api, engine, storage):
    # No Content-Length header at all (chunked transfer): the limit must be enforced on the
    # bytes actually received.
    chunks = [pdf_bytes(1024)] + [b"x" * 1024] * 10
    result = post_chunks(
        api.app, "/v1/uploads", query="filename=big.pdf", headers=PDF_HEADERS, chunks=chunks
    )
    assert result.status == 413
    assert result.body["error"]["code"] == "upload_too_large"
    # It stopped reading as soon as the limit was passed instead of consuming the whole body.
    assert result.chunks_read == TEST_MAX_PDF_BYTES // 1024 + 1
    _assert_rejected(engine, storage, UploadRejectionReason.TOO_LARGE)


def test_lying_content_length_does_not_bypass_the_limit(api, engine, storage):
    chunks = [pdf_bytes(1024)] + [b"x" * 1024] * 10
    result = post_chunks(
        api.app,
        "/v1/uploads",
        query="filename=big.pdf",
        headers={**PDF_HEADERS, "Content-Length": "100"},
        chunks=chunks,
    )
    assert result.status == 413
    _assert_rejected(engine, storage, UploadRejectionReason.TOO_LARGE)


def test_pdf_and_zip_limits_are_independent(api):
    between = (TEST_MAX_PDF_BYTES + TEST_MAX_ZIP_BYTES) // 2
    assert _upload(api, zip_bytes(between), "repo.zip", ZIP_HEADERS).status_code == 201
    assert _upload(api, pdf_bytes(between)).status_code == 413
    assert _upload(api, zip_bytes(TEST_MAX_ZIP_BYTES + 1), "r.zip", ZIP_HEADERS).status_code == 413


# --- Type and content checks ----------------------------------------------------------------------


@pytest.mark.parametrize("filename", ["notes.txt", "setup.exe", "archive.tar.gz", "noextension"])
def test_unsupported_extension_is_rejected_before_anything_is_stored(api, engine, filename):
    response = _upload(api, pdf_bytes(), filename)
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_file_type"
    assert _rows(engine) == []


@pytest.mark.parametrize(
    ("filename", "headers"),
    [
        ("sample.pdf", ZIP_HEADERS),
        ("sample.pdf", {"Content-Type": "application/x-www-form-urlencoded"}),  # curl default
        ("repo.zip", PDF_HEADERS),
        ("sample.pdf", {}),
    ],
)
def test_declared_content_type_must_match_the_extension(api, engine, filename, headers):
    response = _upload(api, pdf_bytes(), filename, headers)
    assert response.status_code == 415
    error = response.json()["error"]
    assert error["code"] == "content_type_mismatch"
    assert error["details"]["accepted_content_types"]
    assert _rows(engine) == []


def test_pdf_named_file_containing_zip_bytes_is_rejected(api, engine, storage):
    response = _upload(api, zip_bytes(), "disguised.pdf")
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "file_signature_mismatch"
    _assert_rejected(engine, storage, UploadRejectionReason.SIGNATURE_MISMATCH)


def test_signature_mismatch_stops_reading_immediately(api, engine, storage):
    chunks = [zip_bytes()] + [b"x" * 100] * 10
    result = post_chunks(
        api.app, "/v1/uploads", query="filename=a.pdf", headers=PDF_HEADERS, chunks=chunks
    )
    assert result.status == 415
    assert result.chunks_read == 1
    _assert_rejected(engine, storage, UploadRejectionReason.SIGNATURE_MISMATCH)


@pytest.mark.parametrize(
    ("content", "filename", "headers"),
    [
        (b"PK\x05\x06" + b"\0" * 18, "empty.zip", ZIP_HEADERS),  # empty archive
        (b"%PD", "short.pdf", PDF_HEADERS),  # shorter than any signature
        (b"just text, not a pdf", "fake.pdf", PDF_HEADERS),
    ],
)
def test_other_signature_mismatches(api, engine, storage, content, filename, headers):
    response = _upload(api, content, filename, headers)
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "file_signature_mismatch"
    _assert_rejected(engine, storage, UploadRejectionReason.SIGNATURE_MISMATCH)


def test_empty_file_is_rejected(api, engine, storage):
    response = _upload(api, b"")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_file"
    _assert_rejected(engine, storage, UploadRejectionReason.EMPTY_FILE)


def test_missing_filename_is_a_validation_error(api, engine):
    response = api.post("/v1/uploads", content=pdf_bytes(), headers=PDF_HEADERS)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["details"]["fields"][0]["field"] == "filename"
    assert _rows(engine) == []


def test_filename_with_control_characters_is_rejected(api, engine):
    response = _upload(api, pdf_bytes(), "bad\x07name.pdf")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_filename"
    assert _rows(engine) == []


# --- Interruptions and failures -------------------------------------------------------------------


def test_client_disconnect_mid_upload_rejects_and_cleans_up(api, engine, storage):
    chunks = [pdf_bytes(512)] + [b"x" * 512] * 3
    result = post_chunks(
        api.app,
        "/v1/uploads",
        query="filename=dropped.pdf",
        headers=PDF_HEADERS,
        chunks=chunks,
        disconnect_after=2,
    )
    assert result.status == 400
    assert result.body["error"]["code"] == "upload_interrupted"
    _assert_rejected(engine, storage, UploadRejectionReason.CLIENT_DISCONNECTED)


def test_failure_after_streaming_rejects_and_cleans_up(api, engine, storage, monkeypatch):
    def broken_commit(self, upload_id, final_relative):
        raise OSError("disk full")

    monkeypatch.setattr(LocalStorage, "commit_incoming", broken_commit)
    response = _upload(api, pdf_bytes())

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert "disk full" not in response.text  # internal details stay in the logs
    _assert_rejected(engine, storage, UploadRejectionReason.INTERNAL_ERROR)


def test_each_upload_gets_its_own_row_even_for_identical_content(api, engine):
    first = _upload(api, pdf_bytes()).json()
    second = _upload(api, pdf_bytes()).json()
    assert first["id"] != second["id"]
    assert first["sha256"] == second["sha256"]
    assert {uuid.UUID(row.id.hex) for row in _rows(engine)} == {
        uuid.UUID(first["id"]),
        uuid.UUID(second["id"]),
    }


# --- Review follow-ups --------------------------------------------------------------------------


def test_signature_split_across_chunks_is_accepted(api, engine, storage):
    content = pdf_bytes()
    chunks = [content[:2], content[2:4], content[4:]]  # "%P", "DF", "-1.4..."
    result = post_chunks(
        api.app, "/v1/uploads", query="filename=split.pdf", headers=PDF_HEADERS, chunks=chunks
    )
    assert result.status == 201
    assert result.body["sha256"] == hashlib.sha256(content).hexdigest()


def test_signature_mismatch_found_in_the_second_chunk(api, engine, storage):
    chunks = [b"%P", b"XX-not-a-pdf", b"x" * 100, b"x" * 100]
    result = post_chunks(
        api.app, "/v1/uploads", query="filename=split.pdf", headers=PDF_HEADERS, chunks=chunks
    )
    assert result.status == 415
    assert result.chunks_read == 2  # stopped as soon as the signature bytes were complete
    _assert_rejected(engine, storage, UploadRejectionReason.SIGNATURE_MISMATCH)


def test_cancelled_request_cleans_up_and_marks_interrupted(api, engine, storage):
    """E.g. the server shutting down mid-upload: cleanup still runs (it is shielded)."""
    chunks = [pdf_bytes(512)] + [b"x" * 512] * 4
    result = post_chunks(
        api.app,
        "/v1/uploads",
        query="filename=cancelled.pdf",
        headers=PDF_HEADERS,
        chunks=chunks,
        cancel_after=2,
    )
    assert result.status is None  # no response: the request was cancelled
    _assert_rejected(engine, storage, UploadRejectionReason.INTERRUPTED)


def test_error_after_ready_committed_keeps_the_file(api, engine, storage, monkeypatch):
    """If marking ready commits but the caller still sees an error (e.g. the connection drops
    right after COMMIT), cleanup must not delete the file of a row that is already 'ready'."""
    from app.services.uploads import UploadService

    original = UploadService._mark_ready

    def mark_ready_then_fail(self, upload_id, sha256, size):
        original(self, upload_id, sha256, size)
        raise ConnectionError("connection lost after commit")

    monkeypatch.setattr(UploadService, "_mark_ready", mark_ready_then_fail)
    response = _upload(api, pdf_bytes())

    assert response.status_code == 500
    [row] = _rows(engine)
    assert row.status == UploadStatus.READY
    assert storage.exists(row.storage_path)  # still usable, so its file is kept


def test_rejection_after_rename_leaves_no_empty_directory(api, engine, storage, monkeypatch):
    from app.services.uploads import UploadService

    def fail(self, upload_id, sha256, size):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(UploadService, "_mark_ready", fail)
    assert _upload(api, pdf_bytes()).status_code == 500
    [row] = _rows(engine)
    assert row.status == UploadStatus.REJECTED
    assert not storage.absolute(row.storage_path).parent.exists()
