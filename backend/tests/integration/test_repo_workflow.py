"""The repo_index workflow at the service level: real database, real stages, real sandboxed ZIP
reader, stub provider. (The Celery round trip is in the e2e tests.)

Acceptance #8: every malicious archive is rejected (or the entry skipped) as specified, and for
every rejection a snapshot of the whole storage root AND its parent folder proves that nothing
was written anywhere except the job's own work folder.
"""

import dataclasses
import hashlib
import os
import stat
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import JobAttempt, JobEvent, OutboxMessage, ProviderCall
from app.db.session import create_session_factory
from app.domain.errors import ERRORS
from app.domain.retry import RetryPolicy
from app.queue.worker_process import workflow_config
from app.services.execution import DropReason, JobExecutionService, RunOutcome
from app.storage.local import LocalStorage
from app.workflows.registry import stages_for
from devtools import zip_samples

KIB = 1024
REPO_STAGES = [
    "validate_archive",
    "extract_files",
    "filter_files",
    "chunk_sources",
    "summarize",
    "write_artifacts",
]
SMALL_LIMITS = {
    "zip_max_entries": 50,
    "zip_max_total_bytes": 512 * KIB,
    "zip_max_file_bytes": 128 * KIB,
    "zip_ratio_check_min_bytes": 16 * KIB,
}
FAST = RetryPolicy(base_seconds=0.001, max_seconds=0.001)


@pytest.fixture
def api_settings(api_settings):
    return api_settings.model_copy(
        update={"max_zip_upload_bytes": 2 * KIB * KIB, "enable_fault_injection": True}
    )


def submit(api, archive: bytes, *, filename: str = "repo.zip", options=None) -> str:
    upload = api.post(
        "/v1/uploads",
        params={"filename": filename},
        content=archive,
        headers={"Content-Type": "application/zip"},
    )
    assert upload.status_code == 201, upload.text
    body = {"workflow_type": "repo_index", "upload_id": upload.json()["id"]}
    if options is not None:
        body["options"] = options
    job = api.post("/v1/jobs", json=body, headers={"Idempotency-Key": f"repo-{uuid.uuid4()}"})
    assert job.status_code == 201, job.text
    return job.json()["id"]


@pytest.fixture
def run_job(engine, api_settings):
    """One attempt, as a worker would run it (after 'publishing' due outbox messages)."""

    def run(job_id: str, *, provider=None, **overrides):
        settings = api_settings.model_copy(update={**SMALL_LIMITS, **overrides})
        config = workflow_config(settings)
        if provider is not None:
            config = dataclasses.replace(config, provider=provider)
        with Session(engine) as session, session.begin():
            session.execute(
                update(OutboxMessage)
                .where(
                    OutboxMessage.job_id == uuid.UUID(job_id),
                    OutboxMessage.published_at.is_(None),
                    OutboxMessage.available_at <= func.now(),
                )
                .values(published_at=func.now(), dispatch_attempts=1)
            )
        service = JobExecutionService(
            create_session_factory(engine),
            worker_id="worker@test:1",
            lease=timedelta(seconds=settings.lease_seconds),
            stages_for=lambda wt: stages_for(wt, config),
            storage=LocalStorage(settings.storage_root),
            retry_policy=FAST,
            fault_injection_enabled=True,
        )
        return service.run(uuid.UUID(job_id))

    return run


def events(engine, job_id: str) -> list[JobEvent]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(JobEvent).where(JobEvent.job_id == uuid.UUID(job_id)).order_by(JobEvent.seq)
            )
        )


def count(engine, model, job_id: str) -> int:
    with Session(engine) as session:
        return session.scalar(
            select(func.count()).select_from(model).where(model.job_id == uuid.UUID(job_id))
        )


def tree(root: Path) -> dict[str, str]:
    """Every file, folder and link under `root` (never following links), with file hashes."""
    found = {}
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(folder) / name
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                kind = hashlib.sha256(path.read_bytes()).hexdigest()
            elif stat.S_ISLNK(info.st_mode):
                kind = f"link -> {os.readlink(path)}"
            else:
                kind = "dir"
            found[str(path.relative_to(root))] = kind
    return found


def manifest_of(api, job_id: str) -> dict:
    detail = api.get(f"/v1/jobs/{job_id}").json()
    url = next(a["download_url"] for a in detail["artifacts"] if a["artifact_type"] == "manifest")
    return api.get(url).json()


# --- Success --------------------------------------------------------------------------------------


def test_a_realistic_repository_is_indexed_and_all_three_artifacts_download(api, engine, run_job):
    job_id = submit(api, zip_samples.repo_zip(huge_bytes=200 * KIB))

    result = run_job(job_id)

    assert result.outcome == RunOutcome.SUCCEEDED
    detail = api.get(f"/v1/jobs/{job_id}").json()
    assert (detail["status"], detail["progress_percent"]) == ("succeeded", 100)
    completed = [
        e.payload["stage"] for e in events(engine, job_id) if e.event_type == "stage.completed"
    ]
    assert completed == REPO_STAGES
    artifacts = {a["artifact_type"]: a for a in detail["artifacts"]}
    assert set(artifacts) == {"summary", "chunks", "manifest"}
    downloads = {kind: api.get(a["download_url"]) for kind, a in artifacts.items()}
    assert all(response.status_code == 200 for response in downloads.values())
    assert downloads["chunks"].headers["content-type"].startswith("application/x-ndjson")
    assert downloads["summary"].text.startswith("# Repository summary of repo.zip")
    manifest = downloads["manifest"].json()
    assert manifest["schema"] == "tasklane.repo_manifest/1"
    assert manifest["totals"]["included"] == 8
    assert len(manifest["skip_reasons"]) == 14  # every skip reason occurs in this repository
    with Session(engine) as session:
        calls = session.scalars(
            select(ProviderCall).where(ProviderCall.job_id == uuid.UUID(job_id))
        ).all()
    assert calls and all(c.stage == "summarize" and c.provider == "stub" for c in calls)
    assert manifest["usage"]["provider_calls"] == len(calls)


def test_a_symlink_to_a_secret_is_skipped_and_never_followed(api, engine, run_job, api_settings):
    secret = api_settings.storage_root.parent / "secret.txt"
    secret.write_text("TOP-SECRET-VALUE")
    job_id = submit(api, zip_samples.symlink_zip())

    assert run_job(job_id).outcome == RunOutcome.SUCCEEDED

    manifest = manifest_of(api, job_id)
    assert {"path": "src/shop/settings.py", "reason": "symlink"} in manifest["skipped"]
    for path in api_settings.storage_root.rglob("*"):
        assert not path.is_symlink()
        if path.is_file():
            assert b"TOP-SECRET-VALUE" not in path.read_bytes()
            assert b"secret.txt" not in path.read_bytes() or path.name == "source.zip"


def test_a_nested_archive_is_skipped_never_extracted(api, run_job, api_settings):
    job_id = submit(api, zip_samples.nested_zip())
    assert run_job(job_id).outcome == RunOutcome.SUCCEEDED
    manifest = manifest_of(api, job_id)
    assert {"path": "vendor-archives/inner.zip", "reason": "nested_archive"} in manifest["skipped"]
    assert "inner/payload.py" not in str(manifest)


# --- Rejections (acceptance #8) -------------------------------------------------------------------


def _absolute() -> bytes:
    return zip_samples.small_repo([("/tmp/evil.txt", b"escaped!")])


def _drive_letter() -> bytes:
    return zip_samples.small_repo([("C:/evil.txt", b"escaped!")])


def _backslash() -> bytes:
    return zip_samples.small_repo([("..\\..\\evil.txt", b"escaped!")])


def _duplicate() -> bytes:
    return zip_samples.small_repo([("README.md", b"a second README")])


def _case_collision() -> bytes:
    return zip_samples.small_repo([("ReadMe.md", b"x")])


def _lying_header() -> bytes:
    data = zip_samples.small_repo([("src/big.txt", b"lorem ipsum " * 17_000)])
    return zip_samples.set_declared_size(data, 2, KIB)


def _too_many() -> bytes:
    return zip_samples.build_zip([(f"src/f{i}.py", b"x = 1\n") for i in range(51)])


REJECTED = {
    "zip slip": (zip_samples.zipslip_zip, "zip_unsafe_path", "validate_archive"),
    "absolute path": (_absolute, "zip_unsafe_path", "validate_archive"),
    "drive letter": (_drive_letter, "zip_unsafe_path", "validate_archive"),
    "backslash traversal": (_backslash, "zip_unsafe_path", "validate_archive"),
    "duplicate names": (_duplicate, "zip_unsafe_path", "validate_archive"),
    "case collision": (_case_collision, "zip_unsafe_path", "validate_archive"),
    "encrypted entry": (zip_samples.encrypted_zip, "zip_encrypted", "validate_archive"),
    "truncated": (zip_samples.corrupt_zip, "zip_corrupt", "validate_archive"),
    "lying header": (_lying_header, "zip_corrupt", "extract_files"),
    "high-ratio bomb": (lambda: zip_samples.bomb_zip(100 * KIB), "zip_bomb", "extract_files"),
    "too many entries": (_too_many, "zip_too_many_files", "validate_archive"),
    "nothing to index": (zip_samples.empty_repo_zip, "repo_no_sources", "filter_files"),
}


@pytest.mark.parametrize(("build", "error_class", "stage"), REJECTED.values(), ids=REJECTED.keys())
def test_malicious_archives_fail_permanently_and_write_nothing_outside_the_work_folder(
    api, engine, run_job, api_settings, build, error_class, stage
):
    job_id = submit(api, build(), filename="evil.zip")
    watched = api_settings.storage_root.parent  # the storage root and everything next to it
    before = tree(watched)

    result = run_job(job_id)

    assert (result.outcome, result.error_class) == (RunOutcome.FAILED, error_class)
    detail = api.get(f"/v1/jobs/{job_id}").json()
    assert detail["status"] == "failed"
    assert detail["error_class"] == error_class
    # The fixed, safe text: no names or content from inside the archive.
    assert detail["error_summary"] == ERRORS[error_class].message.format(
        entries=51, limit=50, ratio=100, files=4
    )
    assert "evil" not in detail["error_summary"]
    assert detail["artifacts"] == []
    [attempt] = detail["attempts"]
    assert attempt["outcome"] == "failed_permanent"
    failed = [e.payload for e in events(engine, job_id) if e.event_type == "stage.failed"]
    assert [(f["stage"], f["error_class"], f["retryable"]) for f in failed] == [
        (stage, error_class, False)
    ]
    # Never retried, and a manual retry isn't offered for input errors.
    assert count(engine, JobAttempt, job_id) == 1
    assert count(engine, OutboxMessage, job_id) == 1
    assert not any(e.payload.get("to") == "retry_wait" for e in events(engine, job_id))

    # Nothing was created, changed or deleted outside work/{job_id}/ ...
    work_parent = os.path.join("storage", "work")  # holds every job's folder; may be created
    work = os.path.join(work_parent, job_id)
    after = tree(watched)

    def outside(snapshot: dict[str, str]) -> dict[str, str]:
        return {
            k: v
            for k, v in snapshot.items()
            if k != work_parent and k != work and not k.startswith(work + os.sep)
        }

    assert outside(after) == outside(before)
    # ... no file named after an archive entry exists anywhere, even inside it ...
    assert not any(Path(k).name == "evil.txt" for k in tree(watched.parent))
    # ... and nothing is left in the work folder but generated names.
    inside = {Path(k).name for k in after if k.startswith(work + os.sep)}
    assert inside <= {"archive.json", "files.bin", "extract.json"}


def test_too_many_summarization_calls_fail_with_repo_too_large(api, run_job):
    job_id = submit(api, zip_samples.repo_zip(huge_bytes=1))
    result = run_job(job_id, repo_max_provider_calls=3)
    assert result.error_class == "repo_too_large"
    assert api.get(f"/v1/jobs/{job_id}").json()["error_summary"] == (
        "The repository has too much source code to summarize within the limit of 3 "
        "summarization calls."
    )


# --- Retries and resume ---------------------------------------------------------------------------


def test_a_transient_failure_retries_and_resumes_after_the_finished_stages(api, engine, run_job):
    job_id = submit(
        api,
        zip_samples.small_repo(),
        options={"fault_injection": {"mode": "transient", "failures": 1}},
    )
    outcomes = []
    for _ in range(10):
        result = run_job(job_id)
        if result.outcome == RunOutcome.DROPPED and result.drop_reason == DropReason.RETRY_NOT_DUE:
            continue
        outcomes.append(result.outcome)
        if result.outcome != RunOutcome.RETRY_SCHEDULED:
            break
    assert outcomes == [RunOutcome.RETRY_SCHEDULED, RunOutcome.SUCCEEDED]

    history = events(engine, job_id)
    failed = [e.payload for e in history if e.event_type == "stage.failed"]
    assert failed == [
        {"stage": "summarize", "error_class": "injected_transient", "retryable": True}
    ]
    skipped = [e.payload for e in history if e.event_type == "stage.skipped"]
    assert skipped == [
        {"stage": stage, "reason": "checkpoint_reused"}
        for stage in ["validate_archive", "extract_files", "filter_files", "chunk_sources"]
    ]
    detail = api.get(f"/v1/jobs/{job_id}").json()
    assert [(a["attempt_no"], a["outcome"]) for a in detail["attempts"]] == [
        (1, "failed_transient"),
        (2, "succeeded"),
    ]
    assert {a["artifact_type"] for a in detail["artifacts"]} == {"summary", "chunks", "manifest"}


def test_a_permanent_failure_is_never_retried(api, engine, run_job):
    job_id = submit(
        api, zip_samples.small_repo(), options={"fault_injection": {"mode": "permanent"}}
    )
    result = run_job(job_id)
    assert (result.outcome, result.error_class) == (RunOutcome.FAILED, "injected_permanent")
    assert count(engine, JobAttempt, job_id) == 1
    assert count(engine, OutboxMessage, job_id) == 1
