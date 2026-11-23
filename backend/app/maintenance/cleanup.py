"""Retention and cleanup: deletes what Tasklane no longer needs, never what it still does.

Run it with:

    python -m app.maintenance.cleanup --dry-run   # print what would be deleted; change nothing
    python -m app.maintenance.cleanup             # delete it

With CLEANUP_ENABLED=true the dispatcher process also runs it every CLEANUP_INTERVAL_SECONDS,
in its own thread (like the recovery loop). Off by default.

What it removes (retention settings in app/config.py, defaults in brackets):
1. Finished jobs (succeeded, failed, cancelled) older than RETENTION_FINISHED_JOB_DAYS [30]:
   the row and, by ON DELETE CASCADE, its events, attempts, artifacts, provider calls,
   checkpoints and outbox rows; then its work/ and artifacts/ folders. Its idempotency key
   becomes free again.
2. Uploads still 'receiving' after RETENTION_INTERRUPTED_UPLOAD_MINUTES [60]: marked
   rejected (interrupted); then their .part and final files.
3. Expired uploads (UPLOAD_TTL_HOURS) that no unfinished job uses: the row is deleted, or marked
   deleted if finished jobs still point at it (their pages show its filename); then the file.
4. Published outbox rows older than RETENTION_PUBLISHED_OUTBOX_HOURS [24].
5. worker_heartbeats rows of processes not seen for RETENTION_WORKER_HEARTBEAT_DAYS [7].
6. Files: work/{job_id}/ of jobs finished longer than RETENTION_WORK_DAYS [7] ago (their
   checkpoint rows are deleted first, so a manual retry reruns every stage); stray `.*.tmp` files
   of finished jobs; files in artifacts/{job_id}/ of a finished job that no artifact row
   references (folders of attempts that never registered artifacts, the pre-Phase 11 layout);
   and any upload/work/artifact folder whose row no longer exists.

Safety rules:
- An unfinished job (queued, running, retry_wait, cancel_requested) and everything it uses are
  never touched: its rows don't match any query, its folders are skipped, and its upload is not
  "unused".
- Rows first, files after: files are deleted only once the rows that point at them are deleted
  or marked in a committed transaction. A crash in between leaves files without rows, which the
  next run removes (it looks for folders whose row is gone). The work folder of a job that still
  exists is removed while its row is locked, so a manual retry can't start in the middle.
- Safe to run twice or at the same time: rows are locked FOR UPDATE SKIP LOCKED (each run takes
  different rows; a row someone else is busy with waits for the next run), deleting a file that
  is already gone is fine, and every row a run handles stops matching its query.
- Bounded: one transaction per CLEANUP_BATCH_SIZE rows (or one job, for folders).
- Paths: only generated names (UUIDs) in the known folders are considered; every path is
  resolved against the storage root and refused if it leaves it or passes through a symlink.
"""

import argparse
import logging
import sys
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.repositories import retention as rows
from app.domain.dispatch import dispatch_backoff_seconds
from app.domain.enums import UploadStatus
from app.storage.local import LocalStorage

logger = logging.getLogger("tasklane.cleanup")

GONE_UPLOAD_STATUSES = frozenset({UploadStatus.DELETED.value, UploadStatus.REJECTED.value})


@dataclass(frozen=True)
class RemovedPath:
    path: str  # relative to the storage root; folders end with "/"
    reason: str
    bytes: int


@dataclass
class CleanupReport:
    dry_run: bool
    # category -> ids of the rows deleted or marked (or that would be, in a dry run)
    rows: dict[str, list[str]] = field(default_factory=dict)
    paths: list[RemovedPath] = field(default_factory=list)
    errors: int = 0

    @property
    def bytes_freed(self) -> int:
        return sum(item.bytes for item in self.paths)

    def counts(self) -> dict[str, int]:
        return {name: len(ids) for name, ids in self.rows.items()}

    def changed_anything(self) -> bool:
        return bool(self.paths) or any(self.rows.values())


@dataclass(frozen=True)
class Retention:
    finished_jobs: timedelta
    work: timedelta
    interrupted_uploads: timedelta
    published_outbox: timedelta
    worker_rows: timedelta
    batch_size: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "Retention":
        return cls(
            finished_jobs=timedelta(days=settings.retention_finished_job_days),
            work=timedelta(days=settings.retention_work_days),
            interrupted_uploads=timedelta(minutes=settings.retention_interrupted_upload_minutes),
            published_outbox=timedelta(hours=settings.retention_published_outbox_hours),
            worker_rows=timedelta(days=settings.retention_worker_heartbeat_days),
            batch_size=settings.cleanup_batch_size,
        )


class Cleanup:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        storage: LocalStorage,
        retention: Retention,
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.retention = retention

    def run(self, *, dry_run: bool = False) -> CleanupReport:
        report = CleanupReport(dry_run=dry_run)
        r = self.retention
        jobs = self._rows(
            report,
            "finished_jobs",
            lambda s, **kw: rows.select_old_finished_jobs(s, r.finished_jobs, **kw),
            rows.delete_jobs,
        )
        interrupted = self._rows(
            report,
            "interrupted_uploads",
            lambda s, **kw: rows.select_interrupted_uploads(s, r.interrupted_uploads, **kw),
            lambda s, ids: [rows.mark_upload_interrupted(s, i) for i in ids],
        )
        expired = self._rows(
            report,
            "expired_uploads",
            lambda s, **kw: rows.select_expired_uploads(s, **kw),
            lambda s, uploads: [_remove_upload_row(s, u.id) for u in uploads],
            key=lambda upload: upload.id,
        )
        self._rows(
            report,
            "published_outbox",
            lambda s, **kw: rows.select_published_outbox(s, r.published_outbox, **kw),
            rows.delete_outbox,
        )
        self._rows(
            report,
            "worker_rows",
            lambda s, **kw: rows.select_old_worker_rows(s, r.worker_rows, **kw),
            rows.delete_worker_rows,
        )
        # Files. A real run decides from the committed rows alone. A dry run changed nothing,
        # so it passes along the rows it *would* have deleted or marked.
        gone_jobs = set(jobs) if dry_run else set()
        gone_uploads = set(interrupted) | {u.id for u in expired} if dry_run else set()
        self._sweep_uploads(report, gone_uploads)
        self._sweep_job_folders(report, "work", gone_jobs)
        self._sweep_job_folders(report, "artifacts", gone_jobs)
        return report

    # --- Rows -------------------------------------------------------------------------------------

    def _rows(
        self,
        report: CleanupReport,
        category: str,
        select_rows: Callable[..., list[Any]],
        act: Callable[[Session, list[Any]], object],
        *,
        key: Callable[[Any], Any] = lambda row: row,
    ) -> list[Any]:
        """Select a batch (locked unless dry run), act on it, commit; repeat until none left."""
        handled: list[Any] = []
        after = None
        while True:
            with self.session_factory.begin() as session:
                batch = select_rows(
                    session,
                    limit=self.retention.batch_size,
                    after=after,
                    lock=not report.dry_run,
                )
                if not batch:
                    break
                if not report.dry_run:
                    act(session, batch)
            handled.extend(batch)
            after = key(batch[-1])
        report.rows[category] = [str(key(row)) for row in handled]
        return handled

    # --- Files ------------------------------------------------------------------------------------

    def _sweep_uploads(self, report: CleanupReport, gone: set[uuid.UUID]) -> None:
        """uploads/{id}/ and uploads/.incoming/{id}.part whose row is gone, deleted or rejected
        (a .part file also once its upload is no longer receiving)."""
        folders = _uuid_names(self.storage.list_dir("uploads"))
        for batch in _chunks(folders, self.retention.batch_size):
            with self.session_factory() as session:
                statuses = rows.upload_statuses(session, batch)
            for upload_id in batch:
                status = statuses.get(upload_id)
                if upload_id in gone or status is None or status in GONE_UPLOAD_STATUSES:
                    self._remove(report, f"uploads/{upload_id}/", "upload no longer kept")
        parts = {
            upload_id: name
            for name in self.storage.list_dir("uploads/.incoming")
            if name.endswith(".part") and (upload_id := _as_uuid(name[: -len(".part")]))
        }
        for batch in _chunks(sorted(parts), self.retention.batch_size):
            with self.session_factory() as session:
                statuses = rows.upload_statuses(session, batch)
            for upload_id in batch:
                if upload_id in gone or statuses.get(upload_id) != UploadStatus.RECEIVING:
                    path = f"uploads/.incoming/{parts[upload_id]}"
                    self._remove(report, path, "unfinished upload no longer receiving")

    def _sweep_job_folders(self, report: CleanupReport, area: str, gone: set[uuid.UUID]) -> None:
        """work/{job_id}/ or artifacts/{job_id}/ folders: see the module docstring."""
        for batch in _chunks(_uuid_names(self.storage.list_dir(area)), self.retention.batch_size):
            with self.session_factory() as session:
                jobs = rows.job_files(session, batch, self.retention.work, lock=False)
            for job_id in batch:
                folder = f"{area}/{job_id}/"
                files = jobs.get(job_id)
                if job_id in gone or files is None:
                    self._remove(report, folder, "job deleted")
                elif not files.finished:
                    continue  # never touch an unfinished job's files
                elif area == "work" and files.work_expired:
                    # Errors here (e.g. the session ended mid-delete) only skip this folder.
                    self._safely(
                        report, lambda job_id=job_id: self._remove_work_folder(report, job_id)
                    )
                else:
                    self._safely(
                        report,
                        lambda job_id=job_id: self._remove_unreferenced(report, area, job_id),
                    )

    def _remove_work_folder(self, report: CleanupReport, job_id: uuid.UUID) -> None:
        folder = f"work/{job_id}/"
        reason = "work folder of a job finished before the retention"
        if report.dry_run:
            self._remove(report, folder, reason)
            return
        # 1. Delete the checkpoint rows (they describe these files) and commit.
        with self._locked_finished_job(job_id) as (session, files):
            if files is None or not files.work_expired:
                return  # retried (and maybe finished again) since, or busy: next run
            rows.delete_checkpoints(session, job_id)
        # 2. Delete the folder while holding the job's lock, so a manual retry can't start a new
        #    attempt that writes into it meanwhile.
        with self._locked_finished_job(job_id) as (_session, files):
            if files is not None and files.work_expired:
                self._remove(report, folder, reason)

    def _remove_unreferenced(self, report: CleanupReport, area: str, job_id: uuid.UUID) -> None:
        """A finished job's files that nothing uses: in artifacts/, files no artifact row
        references; in work/, temporary files left by a crash mid-write."""
        candidates = [
            path
            for path in self._files(report, f"{area}/{job_id}")
            if area == "artifacts" or _is_temp(path)
        ]
        if not candidates:
            return
        with self._locked_finished_job(job_id, dry_run=report.dry_run) as (_session, files):
            if files is None:
                return
            for path in candidates:
                if path not in files.artifact_paths:
                    reason = "temporary file" if _is_temp(path) else "artifact not registered"
                    self._remove(report, path, reason)
        if not report.dry_run:
            self._safely(report, lambda: self.storage.remove_empty_dirs(f"{area}/{job_id}"))

    # --- Helpers ----------------------------------------------------------------------------------

    @contextmanager
    def _locked_finished_job(
        self, job_id: uuid.UUID, *, dry_run: bool = False
    ) -> Iterator[tuple[Session, rows.JobFiles | None]]:
        """A transaction holding the job's row lock while the job is finished. Yields its
        JobFiles, or None if it is gone, unfinished, or locked by someone else (SKIP LOCKED:
        left for the next run). A dry run reads without locking."""
        with self.session_factory.begin() as session:
            files = rows.job_files(session, [job_id], self.retention.work, lock=not dry_run)
            found = files.get(job_id)
            yield session, (found if found is not None and found.finished else None)

    def _files(self, report: CleanupReport, relative: str) -> list[str]:
        result: list[str] = []
        self._safely(report, lambda: result.extend(self.storage.files_under(relative)))
        return result

    def _remove(self, report: CleanupReport, path: str, reason: str) -> None:
        def remove() -> None:
            relative = path.rstrip("/")
            if report.dry_run:
                if path.endswith("/"):
                    self.storage.checked(relative)  # refuse what a real run would refuse
                size = self.storage.size_of(relative)
            elif path.endswith("/"):
                size = self.storage.remove_tree(relative)
            else:
                size = self.storage.remove_file(relative)
            if size is not None:  # None: already gone (e.g. a concurrent run removed it)
                report.paths.append(RemovedPath(path, reason, size))

        self._safely(report, remove)

    def _safely(self, report: CleanupReport, action: Callable[[], object]) -> None:
        """One path that can't be handled (refused, permissions) must not stop the others."""
        try:
            action()
        except FileNotFoundError:
            pass  # removed meanwhile (by a concurrent run): nothing left to do
        except Exception:
            report.errors += 1
            logger.exception("cleanup could not handle a path; skipping it")


def _remove_upload_row(session: Session, upload_id: uuid.UUID) -> None:
    statuses = rows.lock_jobs_of_upload(session, upload_id)
    if any(status not in rows.FINISHED for status in statuses):
        return  # reopened by a manual retry since it was selected: keep it
    if not rows.delete_unreferenced_upload(session, upload_id):
        rows.mark_upload_deleted(session, upload_id)  # finished jobs still show its filename


def _as_uuid(name: str) -> uuid.UUID | None:
    try:
        value = uuid.UUID(name)
    except ValueError:
        return None
    return value if str(value) == name else None  # only our own canonical names


def _uuid_names(names: Sequence[str]) -> list[uuid.UUID]:
    return [value for name in names if (value := _as_uuid(name)) is not None]


def _chunks(items: Sequence[uuid.UUID], size: int) -> Iterator[list[uuid.UUID]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _is_temp(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith(".") and name.endswith(".tmp")


# --- Running it -----------------------------------------------------------------------------------


def run_forever(
    cleanup: Cleanup,
    *,
    interval_seconds: float,
    stop: threading.Event,
    backoff: Callable[[int], float] = dispatch_backoff_seconds,
) -> None:
    """The dispatcher's cleanup loop: a run every `interval_seconds` until `stop` is set."""
    consecutive_failures = 0
    while not stop.is_set():
        try:
            report = cleanup.run()
        except Exception:
            consecutive_failures += 1
            logger.exception("cleanup run failed; backing off")
            stop.wait(backoff(consecutive_failures))
            continue
        consecutive_failures = 0
        if report.changed_anything() or report.errors:
            logger.info(
                "cleanup finished",
                extra={
                    "rows": report.counts(),
                    "paths": len(report.paths),
                    "bytes_freed": report.bytes_freed,
                    "errors": report.errors,
                },
            )
        stop.wait(interval_seconds)


def start_in_thread(
    cleanup: Cleanup, *, interval_seconds: float, stop: threading.Event
) -> threading.Thread:
    thread = threading.Thread(
        target=run_forever,
        args=(cleanup,),
        kwargs={"interval_seconds": interval_seconds, "stop": stop},
        name="cleanup",
        daemon=True,
    )
    thread.start()
    return thread


ROW_LABELS = {
    "finished_jobs": "finished jobs deleted (with their events, attempts, artifacts, ...)",
    "interrupted_uploads": "interrupted uploads marked rejected",
    "expired_uploads": "expired uploads deleted or marked deleted",
    "published_outbox": "published outbox rows deleted",
    "worker_rows": "worker_heartbeats rows deleted",
}


def format_report(report: CleanupReport) -> str:
    verb = "would be" if report.dry_run else "were"
    lines = [
        "Tasklane cleanup" + (" (DRY RUN: nothing was changed)" if report.dry_run else ""),
        "",
        "Rows:",
    ]
    for category, ids in report.rows.items():
        lines.append(f"  {len(ids):>6}  {ROW_LABELS[category]}")
        lines.extend(f"          {row_id}" for row_id in ids)
    lines += [
        "",
        f"Files and folders that {verb} removed: {len(report.paths)} "
        f"({_human(report.bytes_freed)})",
    ]
    lines.extend(f"  {item.path}  ({item.reason}, {_human(item.bytes)})" for item in report.paths)
    if report.errors:
        lines += ["", f"{report.errors} path(s) could not be handled; see the log above."]
    return "\n".join(lines)


def _human(size: int) -> str:
    for unit, factor in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if size >= factor:
            return f"{size / factor:.1f} {unit}"
    return f"{size} bytes"


def main(argv: Sequence[str] | None = None) -> int:
    from app.config import get_settings
    from app.db.session import create_db_engine, create_session_factory
    from app.observability.logging import configure_logging

    parser = argparse.ArgumentParser(
        prog="python -m app.maintenance.cleanup",
        description="Delete finished jobs, expired uploads and leftover files past their "
        "retention (settings: RETENTION_*). Unfinished jobs are never touched.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would be deleted; change nothing"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, secrets=(settings.postgres_password.get_secret_value(),))
    engine = create_db_engine(
        settings, pool_size=1, max_overflow=1, application_name="tasklane-cleanup"
    )
    try:
        report = Cleanup(
            create_session_factory(engine),
            LocalStorage(settings.storage_root),
            Retention.from_settings(settings),
        ).run(dry_run=args.dry_run)
    finally:
        engine.dispose()
    print(format_report(report))
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
