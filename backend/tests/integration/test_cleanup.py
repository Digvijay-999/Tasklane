"""Retention and cleanup (app/maintenance/cleanup.py) against the real database and a real
storage folder: every category, the dry run, unfinished jobs, concurrent runs, crash leftovers,
and a snapshot of the whole disk area proving nothing else is removed."""

import os
import threading
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.models import (
    Artifact,
    Job,
    JobAttempt,
    JobEvent,
    OutboxMessage,
    ProviderCall,
    StageCheckpoint,
    Upload,
    WorkerHeartbeat,
)
from app.db.repositories.retention import select_expired_uploads
from app.db.session import create_session_factory
from app.domain.enums import AttemptOutcome, JobStatus, UploadRejectionReason, UploadStatus
from app.maintenance.cleanup import (
    Cleanup,
    CleanupReport,
    Retention,
    _remove_upload_row,
    format_report,
)
from app.maintenance.cleanup import start_in_thread as start_cleanup_thread
from tests.factories import (
    make_artifact,
    make_attempt,
    make_checkpoint,
    make_event,
    make_job,
    make_outbox,
    make_provider_call,
    make_upload,
    make_worker_heartbeat,
    utcnow,
)

S = JobStatus
DAY = timedelta(days=1)
RETENTION = Retention(
    finished_jobs=30 * DAY,
    work=7 * DAY,
    interrupted_uploads=timedelta(minutes=60),
    published_outbox=timedelta(hours=24),
    worker_rows=7 * DAY,
    batch_size=2,  # small batches, so paging over several transactions is exercised
)
ACTIVE = [S.QUEUED, S.RUNNING, S.RETRY_WAIT, S.CANCEL_REQUESTED]
OWNED_BY_JOB = [JobAttempt, JobEvent, Artifact, ProviderCall, StageCheckpoint, OutboxMessage]


@pytest.fixture
def session_factory(engine):
    return create_session_factory(engine)


@pytest.fixture
def cleanup(session_factory, storage) -> Cleanup:
    return Cleanup(session_factory, storage, RETENTION)


# --- Builders -------------------------------------------------------------------------------------


def ago(delta: timedelta):
    return utcnow() - delta


def put(storage, relative: str, data: bytes = b"data") -> str:
    path = storage.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return relative


def tree(root: Path) -> set[str]:
    """Every file, symlink and folder below root (relative; folders end with "/")."""
    found = set()
    for folder, dirs, files in os.walk(root):
        found |= {Path(folder, name).relative_to(root).as_posix() + "/" for name in dirs}
        found |= {Path(folder, name).relative_to(root).as_posix() for name in files}
    return found


def new_upload(session, storage, *, status=UploadStatus.READY, expired=False, **overrides):
    upload_id = uuid.uuid4()
    values = {"id": upload_id, "storage_path": f"uploads/{upload_id}/source.pdf", "status": status}
    if expired:
        values.update(created_at=ago(3 * DAY), expires_at=ago(2 * DAY))
    if status == UploadStatus.RECEIVING:
        values.update(size_bytes=None, sha256=None)
    if status == UploadStatus.REJECTED:
        values["rejection_reason"] = UploadRejectionReason.TOO_LARGE
    values.update(overrides)
    upload = make_upload(session, **values)
    if status in (UploadStatus.READY, UploadStatus.RECEIVING):
        put(storage, upload.storage_path)
    return upload


def new_job(session, storage, status=S.SUCCEEDED, *, finished=None, upload=None, **overrides):
    """A job with one row of everything a job owns, and its files: work/{id}/ (two stage
    outputs) and artifacts/{id}/attempt-1/summary.md (registered)."""
    if finished is not None:
        overrides["finished_at"] = ago(finished)
    job = make_job(
        session, status=status, upload=upload or new_upload(session, storage), **overrides
    )
    if status != S.QUEUED:
        closed = status not in (S.RUNNING, S.CANCEL_REQUESTED)
        make_attempt(
            session,
            job,
            ended_at=utcnow() if closed else None,
            outcome=AttemptOutcome.SUCCEEDED if closed else None,
        )
    make_event(session, job)
    make_provider_call(session, job)
    make_checkpoint(session, job)
    make_outbox(session, job, published_at=utcnow())
    summary = make_artifact(session, job, storage_path=f"artifacts/{job.id}/attempt-1/summary.md")
    put(storage, f"work/{job.id}/validate.json")
    put(storage, f"work/{job.id}/files.bin", b"x" * 1000)
    put(storage, summary.storage_path)
    return job


def owned_rows(session, job_id) -> int:
    return sum(
        session.scalar(select(func.count()).select_from(model).where(model.job_id == job_id))
        for model in OWNED_BY_JOB
    )


def table_counts(engine) -> dict[str, int]:
    with Session(engine) as session:
        return {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in [Job, Upload, WorkerHeartbeat, *OWNED_BY_JOB]
        }


def in_tx(engine):
    session = Session(engine, expire_on_commit=False)
    session.begin()
    return session


def paths(report: CleanupReport) -> set[str]:
    return {item.path for item in report.paths}


# --- Each category --------------------------------------------------------------------------------


def test_finished_jobs_past_retention_are_deleted_with_everything_they_own(
    engine, cleanup, storage
):
    with in_tx(engine) as session:
        old = [
            new_job(session, storage, status, finished=40 * DAY)
            for status in (S.SUCCEEDED, S.FAILED, S.CANCELLED)
        ]
        recent = new_job(session, storage, finished=2 * DAY)
        old_keys = [job.idempotency_key for job in old]
        session.commit()

    report = cleanup.run()

    assert set(report.rows["finished_jobs"]) == {str(job.id) for job in old}
    with Session(engine) as session:
        for job in old:
            assert session.get(Job, job.id) is None
            assert owned_rows(session, job.id) == 0
        assert session.get(Job, recent.id) is not None
        assert owned_rows(session, recent.id) == len(OWNED_BY_JOB)
    for job in old:
        assert f"work/{job.id}/" in paths(report)
        assert f"artifacts/{job.id}/" in paths(report)
        assert not (storage.root / "work" / str(job.id)).exists()
        assert not (storage.root / "artifacts" / str(job.id)).exists()
    assert (storage.root / "work" / str(recent.id) / "files.bin").exists()
    # The deleted jobs' idempotency keys can be used again.
    with Session(engine) as session, session.begin():
        for key in old_keys:
            make_job(session, idempotency_key=key)


def test_interrupted_uploads_are_rejected_and_their_files_removed(engine, cleanup, storage):
    with in_tx(engine) as session:
        stuck = new_upload(session, storage, status=UploadStatus.RECEIVING, created_at=ago(2 * DAY))
        put(storage, f"uploads/.incoming/{stuck.id}.part")
        streaming = new_upload(session, storage, status=UploadStatus.RECEIVING)
        put(storage, f"uploads/.incoming/{streaming.id}.part")
        session.commit()

    report = cleanup.run()

    assert report.rows["interrupted_uploads"] == [str(stuck.id)]
    with Session(engine) as session:
        row = session.get(Upload, stuck.id)
        assert (row.status, row.rejection_reason) == ("rejected", "interrupted")
        assert session.get(Upload, streaming.id).status == "receiving"
    assert paths(report) == {f"uploads/{stuck.id}/", f"uploads/.incoming/{stuck.id}.part"}
    assert (storage.root / f"uploads/.incoming/{streaming.id}.part").exists()
    assert (storage.root / streaming.storage_path).exists()


def test_expired_uploads_unused_by_unfinished_jobs_are_removed(engine, cleanup, storage):
    with in_tx(engine) as session:
        unused = new_upload(session, storage, expired=True)
        rejected = new_upload(session, storage, status=UploadStatus.REJECTED, expired=True)
        for_finished = new_upload(session, storage, expired=True)
        finished = new_job(session, storage, S.FAILED, upload=for_finished)
        for_running = new_upload(session, storage, expired=True)
        new_job(session, storage, S.RUNNING, upload=for_running)
        fresh = new_upload(session, storage)
        session.commit()

    report = cleanup.run()

    assert set(report.rows["expired_uploads"]) == {
        str(unused.id),
        str(rejected.id),
        str(for_finished.id),
    }
    with Session(engine) as session:
        assert session.get(Upload, unused.id) is None
        assert session.get(Upload, rejected.id) is None
        # Still shown on the failed job's page, so the row stays (marked deleted).
        marked = session.get(Upload, for_finished.id)
        assert marked.status == "deleted" and marked.deleted_at is not None
        assert session.get(Job, finished.id) is not None
        assert session.get(Upload, for_running.id).status == "ready"
        assert session.get(Upload, fresh.id).status == "ready"
    assert {f"uploads/{unused.id}/", f"uploads/{for_finished.id}/"} <= paths(report)
    assert (storage.root / for_running.storage_path).exists()
    assert (storage.root / fresh.storage_path).exists()

    # Once the failed job is gone too, the marked row is deleted on the next run.
    with Session(engine) as session, session.begin():
        session.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": finished.id})
    assert cleanup.run().rows["expired_uploads"] == [str(for_finished.id)]


def test_an_upload_reopened_by_a_manual_retry_after_selection_is_kept(engine, storage):
    """The cleanup selected an expired upload whose only job had failed; before it acted, a
    manual retry put the job back in the queue. The re-check under the jobs' share locks keeps
    the upload (and so its file)."""
    with in_tx(engine) as session:
        upload = new_upload(session, storage, expired=True)
        job = make_job(session, status=S.FAILED, upload=upload)
        session.commit()
    with Session(engine) as session, session.begin():
        [selected] = select_expired_uploads(session, limit=10, lock=True)
        assert selected.id == upload.id
        # ...meanwhile the retry commits (simulated here: the job is queued again).
        with Session(engine) as other, other.begin():
            other.execute(
                text(
                    "UPDATE jobs SET status = 'queued', finished_at = NULL, error_class = NULL, "
                    "error_summary = NULL WHERE id = :id"
                ),
                {"id": job.id},
            )
        _remove_upload_row(session, selected.id)
    with Session(engine) as session:
        assert session.get(Upload, upload.id).status == "ready"


def test_work_folders_of_jobs_finished_before_the_work_retention(engine, cleanup, storage):
    with in_tx(engine) as session:
        older = new_job(session, storage, finished=10 * DAY)  # past work (7), not job (30) days
        recent = new_job(session, storage, finished=1 * DAY)
        temp = [
            put(storage, f"work/{recent.id}/.chunks.jsonl.0a1b.tmp"),
            put(storage, f"work/{recent.id}/summaries/.c1.json.2c3d.tmp"),
        ]
        put(storage, f"work/{recent.id}/summaries/c1.json")
        session.commit()

    report = cleanup.run()

    assert paths(report) == {f"work/{older.id}/", *temp}
    with Session(engine) as session:
        # Its checkpoints described the deleted files: gone, so a manual retry reruns stages.
        assert session.scalar(select(func.count()).where(StageCheckpoint.job_id == older.id)) == 0
        assert session.get(Job, older.id) is not None  # the job and its artifacts stay
        assert session.scalar(select(func.count()).where(Artifact.job_id == older.id)) == 1
        assert session.scalar(select(func.count()).where(StageCheckpoint.job_id == recent.id)) == 1
    assert (storage.root / f"artifacts/{older.id}/attempt-1/summary.md").exists()
    assert (storage.root / f"work/{recent.id}/summaries/c1.json").exists()
    assert report.bytes_freed == 4 + 1000 + 4 + 4


def test_artifact_files_no_row_references_are_removed(engine, cleanup, storage):
    with in_tx(engine) as session:
        job = new_job(session, storage, finished=1 * DAY)
        unregistered = [
            put(storage, f"artifacts/{job.id}/attempt-2/summary.md"),  # failed write_artifacts
            put(storage, f"artifacts/{job.id}/attempt-2/manifest.json"),
            put(storage, f"artifacts/{job.id}/attempt-1/.manifest.json.9f.tmp"),
            put(storage, f"artifacts/{job.id}/summary.md"),  # pre-Phase 11 layout
        ]
        session.commit()

    report = cleanup.run()

    assert paths(report) == set(unregistered)
    assert tree(storage.root / "artifacts" / str(job.id)) == {"attempt-1/", "attempt-1/summary.md"}


def test_published_outbox_and_old_worker_rows(engine, cleanup, storage):
    with in_tx(engine) as session:
        job = make_job(session, status=S.QUEUED)
        old = make_outbox(session, job, published_at=ago(2 * DAY))
        recent = make_outbox(session, job, published_at=ago(timedelta(hours=1)))
        pending = make_outbox(session, job, created_at=ago(3 * DAY), available_at=ago(3 * DAY))
        stopped = make_worker_heartbeat(session, last_seen_at=ago(8 * DAY), stopped_at=ago(8 * DAY))
        crashed = make_worker_heartbeat(session, last_seen_at=ago(9 * DAY))
        alive = make_worker_heartbeat(session)
        just_stopped = make_worker_heartbeat(session, stopped_at=utcnow())
        session.commit()

    report = cleanup.run()

    assert report.rows["published_outbox"] == [str(old.id)]
    assert set(report.rows["worker_rows"]) == {stopped.worker_id, crashed.worker_id}
    with Session(engine) as session:
        assert set(session.scalars(select(OutboxMessage.id))) == {recent.id, pending.id}
        assert set(session.scalars(select(WorkerHeartbeat.worker_id))) == {
            alive.worker_id,
            just_stopped.worker_id,
        }


# --- Safety ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ACTIVE)
def test_unfinished_jobs_are_never_touched(engine, session_factory, storage, status):
    """Even with every retention at zero: an unfinished job's rows, its expired upload, its work
    folder (temporary files included) and unregistered artifact files all stay."""
    with in_tx(engine) as session:
        upload = new_upload(session, storage, expired=True)
        job = new_job(session, storage, status, upload=upload, created_at=ago(400 * DAY))
        session.execute(text("DELETE FROM outbox WHERE job_id = :id"), {"id": job.id})
        make_outbox(session, job, created_at=ago(400 * DAY))  # unpublished: due to be sent
        put(storage, f"work/{job.id}/.chunks.jsonl.0a1b.tmp")
        put(storage, f"artifacts/{job.id}/attempt-2/summary.md")
        session.commit()
    before_rows, before_files = table_counts(engine), tree(storage.root)
    zero = Retention(
        finished_jobs=timedelta(0),
        work=timedelta(0),
        interrupted_uploads=timedelta(0),
        published_outbox=timedelta(0),
        worker_rows=timedelta(0),
        batch_size=2,
    )

    report = Cleanup(session_factory, storage, zero).run()

    assert not report.changed_anything() and report.errors == 0
    assert table_counts(engine) == before_rows
    assert tree(storage.root) == before_files


def build_mixed(engine, storage) -> dict[str, set[str]]:
    """One of everything (to delete, and to keep). Returns the rows and paths a run removes."""
    with in_tx(engine) as session:
        old_job = new_job(
            session, storage, finished=40 * DAY, upload=new_upload(session, storage, expired=True)
        )
        old_work = new_job(session, storage, finished=10 * DAY)
        put(storage, f"artifacts/{old_work.id}/attempt-2/summary.md")
        kept = new_job(session, storage, finished=1 * DAY)
        running = new_job(session, storage, S.RUNNING)
        put(storage, f"work/{running.id}/.pages.jsonl.77.tmp")
        stuck = new_upload(session, storage, status=UploadStatus.RECEIVING, created_at=ago(DAY))
        unused = new_upload(session, storage, expired=True)
        outbox = make_outbox(session, kept, published_at=ago(3 * DAY))
        worker = make_worker_heartbeat(session, last_seen_at=ago(30 * DAY))
        session.commit()
        return {
            "finished_jobs": {str(old_job.id)},
            "interrupted_uploads": {str(stuck.id)},
            "expired_uploads": {str(old_job.upload_id), str(unused.id)},
            "published_outbox": {str(outbox.id)},
            "worker_rows": {worker.worker_id},
            "paths": {
                f"work/{old_job.id}/",
                f"artifacts/{old_job.id}/",
                f"uploads/{old_job.upload_id}/",
                f"work/{old_work.id}/",
                f"artifacts/{old_work.id}/attempt-2/summary.md",
                f"uploads/{stuck.id}/",
                f"uploads/{unused.id}/",
            },
        }


def report_sets(report: CleanupReport) -> dict[str, set[str]]:
    return {**{name: set(ids) for name, ids in report.rows.items()}, "paths": paths(report)}


def test_dry_run_prints_what_a_real_run_deletes_and_changes_nothing(engine, cleanup, storage):
    expected = build_mixed(engine, storage)
    before_rows, before_files = table_counts(engine), tree(storage.root)

    dry = cleanup.run(dry_run=True)

    assert report_sets(dry) == expected
    assert table_counts(engine) == before_rows
    assert tree(storage.root) == before_files
    printed = format_report(dry)
    assert "DRY RUN: nothing was changed" in printed
    assert all(path in printed for path in expected["paths"])

    real = cleanup.run()

    assert report_sets(real) == expected
    assert real.bytes_freed == dry.bytes_freed > 0
    assert "would be" not in format_report(real)
    # Idempotent: a second run finds nothing left to do.
    again = cleanup.run()
    assert not again.changed_anything() and again.errors == 0


def test_concurrent_runs_split_the_work_and_end_in_the_same_state(
    engine,
    session_factory,
    storage,
):
    expected = build_mixed(engine, storage)
    with in_tx(engine) as session:  # more rows, so both runs have something to compete for
        for _ in range(6):
            new_job(session, storage, finished=40 * DAY)
        session.commit()
    runs = [Cleanup(session_factory, storage, RETENTION) for _ in range(2)]
    reports: list[CleanupReport] = []
    start = threading.Barrier(2)

    def run(cleanup: Cleanup) -> None:
        start.wait()
        reports.append(cleanup.run())

    threads = [threading.Thread(target=run, args=(c,)) for c in runs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(reports) == 2 and all(r.errors == 0 for r in reports)
    first, second = (report_sets(r) for r in reports)
    # Every row was handled by exactly one of the runs.
    for category in ("finished_jobs", "interrupted_uploads", "published_outbox", "worker_rows"):
        assert not first[category] & second[category], category
    assert len(first["finished_jobs"] | second["finished_jobs"]) == 7
    assert expected["paths"] <= first["paths"] | second["paths"]
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 3  # old_work, kept, running
    assert not any(
        (storage.root / path.rstrip("/")).exists() for path in first["paths"] | second["paths"]
    )


def test_files_left_by_a_crash_after_the_commit_are_removed_next_time(engine, cleanup, storage):
    """Rows are deleted first and files after; a crash in between leaves folders whose rows are
    gone. The next run removes them."""
    with in_tx(engine) as session:
        job = new_job(session, storage, finished=1 * DAY)
        upload = new_upload(session, storage)
        session.commit()
    orphan_part = put(storage, f"uploads/.incoming/{uuid.uuid4()}.part")
    with Session(engine) as session, session.begin():
        session.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job.id})
        session.execute(text("DELETE FROM uploads WHERE id = :id"), {"id": upload.id})

    report = cleanup.run()

    assert paths(report) >= {
        f"work/{job.id}/",
        f"artifacts/{job.id}/",
        f"uploads/{upload.id}/",
        orphan_part,
    }


def test_whole_disk_snapshot_only_the_targeted_paths_disappear(engine, cleanup, storage, tmp_path):
    """A snapshot of the test's whole temporary folder (the storage root AND everything next to
    it) before and after: exactly the expected paths are removed. Unknown names are left alone,
    and a symlink planted where a job folder would be is refused, not followed."""
    expected = build_mixed(engine, storage)
    outside = tmp_path / "outside"
    put_outside = outside / "precious.txt"
    outside.mkdir()
    put_outside.write_text("keep me")
    linked_job = uuid.uuid4()  # no such job: its "folder" would count as leftover
    (storage.root / "work").mkdir(exist_ok=True)
    os.symlink(outside, storage.root / "work" / str(linked_job))
    for unknown in (
        "work/not-a-job/file.txt",
        "uploads/.incoming/notes.txt",
        "README.txt",
        f"artifacts/{str(uuid.uuid4()).upper()}/x.md",  # not our canonical (lowercase) name
    ):
        put(storage, unknown)
    before = tree(tmp_path)

    report = cleanup.run()

    removed = before - tree(tmp_path)
    expected_removed = {
        f"storage/{path}" for path in expected["paths"]
    } | {  # ...and everything that was inside the removed folders
        name
        for name in before
        for path in expected["paths"]
        if path.endswith("/") and name.startswith(f"storage/{path}")
    }
    # Folders left empty after their last file was removed are pruned too.
    pruned = {name for name in removed - expected_removed if name.endswith("/")}
    assert removed - pruned == expected_removed
    assert all(name.startswith("storage/artifacts/") for name in pruned)
    assert put_outside.read_text() == "keep me"
    assert (storage.root / "work" / str(linked_job)).is_symlink()
    assert report.errors == 1  # the symlink, refused and logged


def test_the_dispatcher_loop_runs_cleanup_until_stopped(engine, cleanup, storage):
    with in_tx(engine) as session:
        job = new_job(session, storage, finished=40 * DAY)
        session.commit()
    stop = threading.Event()
    thread = start_cleanup_thread(cleanup, interval_seconds=60, stop=stop)
    try:
        for _ in range(100):
            with Session(engine) as session:
                if session.get(Job, job.id) is None:
                    break
            stop.wait(0.05)
        else:
            pytest.fail("the cleanup loop did not run")
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
