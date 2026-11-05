"""Receiving uploads: stream to disk, check type and size, record the result.

Steps (see docs/api.md):
1. Before reading the body: check the filename, its extension, the declared Content-Type, and
   (if present) Content-Length against the limit, which only lets us fail early.
2. Insert an `uploads` row with status 'receiving' and commit, so even a crash mid-upload
   leaves a record pointing at the files it may have left behind.
3. Stream chunks to a temporary file, hashing and counting bytes as they arrive. The size limit
   is enforced on the bytes actually received; the signature is checked on the first bytes.
4. On success: fsync, rename into the final generated path, mark the row 'ready'.
5. On any failure: delete the files and mark the row 'rejected' with a reason.

Blocking work (disk writes, database calls) runs in worker threads via anyio, so one large
upload doesn't stall the server's event loop.
"""

import hashlib
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import BinaryIO

import anyio
from anyio import to_thread
from sqlalchemy import func
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Upload
from app.db.repositories import uploads as upload_rows
from app.domain.enums import UploadKind, UploadRejectionReason
from app.domain.uploads import (
    ACCEPTED_DECLARED_TYPES,
    DETECTED_CONTENT_TYPE,
    KIND_BY_EXTENSION,
    SIGNATURE_LENGTH,
    declared_type_matches,
    filename_problem,
    kind_from_filename,
    signature_matches,
)
from app.services.errors import ErrorCode, ServiceError
from app.storage.local import LocalStorage

logger = logging.getLogger(__name__)


class UploadInterruptedError(Exception):
    """The client disconnected before the whole file arrived (raised by the api layer)."""


@dataclass(frozen=True)
class UploadLimits:
    pdf_bytes: int
    zip_bytes: int

    def for_kind(self, kind: UploadKind) -> int:
        return self.pdf_bytes if kind == UploadKind.PDF else self.zip_bytes


class _RejectedError(Exception):
    """Internal: the upload must be rejected for `reason`; `error` is what the client sees."""

    def __init__(self, reason: UploadRejectionReason, error: ServiceError) -> None:
        super().__init__(error.message)
        self.reason = reason
        self.error = error


class UploadService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        storage: LocalStorage,
        limits: UploadLimits,
        ttl: timedelta,
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.limits = limits
        self.ttl = ttl

    async def receive(
        self,
        *,
        filename: str,
        declared_content_type: str | None,
        content_length: int | None,
        chunks: AsyncIterator[bytes],
    ) -> Upload:
        kind = self._check_request(filename, declared_content_type)
        limit = self.limits.for_kind(kind)
        if content_length is not None and content_length > limit:
            raise _too_large(kind, limit)  # fail fast; nothing recorded yet

        upload_id = uuid.uuid4()
        final_path = self.storage.upload_path(upload_id, kind)
        await to_thread.run_sync(self._insert_receiving, upload_id, final_path, filename, kind)

        try:
            sha256, size = await self._stream_to_disk(upload_id, kind, limit, chunks)
            await to_thread.run_sync(self.storage.commit_incoming, upload_id, final_path)
            await to_thread.run_sync(self._mark_ready, upload_id, sha256, size)
        except _RejectedError as rejected:
            await self._reject(upload_id, final_path, rejected.reason)
            raise rejected.error from None
        except UploadInterruptedError:
            await self._reject(upload_id, final_path, UploadRejectionReason.CLIENT_DISCONNECTED)
            raise
        except anyio.get_cancelled_exc_class():
            # The request was cancelled (e.g. the server is shutting down): clean up, re-raise.
            await self._reject(upload_id, final_path, UploadRejectionReason.INTERRUPTED)
            raise
        except BaseException:
            # Unexpected: clean up and re-raise. The error middleware logs the traceback once.
            await self._reject(upload_id, final_path, UploadRejectionReason.INTERNAL_ERROR)
            raise

        return await to_thread.run_sync(self._load, upload_id)

    # --- Steps -----------------------------------------------------------------------------------

    @staticmethod
    def _check_request(filename: str, declared_content_type: str | None) -> UploadKind:
        problem = filename_problem(filename)
        if problem:
            raise ServiceError(ErrorCode.INVALID_FILENAME, problem)
        kind = kind_from_filename(filename)
        if kind is None:
            raise ServiceError(
                ErrorCode.UNSUPPORTED_FILE_TYPE,
                "Only .pdf and .zip files can be uploaded.",
                {"allowed_extensions": sorted(KIND_BY_EXTENSION)},
            )
        if not declared_type_matches(kind, declared_content_type):
            accepted = sorted(ACCEPTED_DECLARED_TYPES[kind])
            raise ServiceError(
                ErrorCode.CONTENT_TYPE_MISMATCH,
                f"A .{kind.value} upload must be sent with a Content-Type header of: "
                f"{', '.join(accepted)}.",
                {"accepted_content_types": accepted},
            )
        return kind

    def _insert_receiving(
        self, upload_id: uuid.UUID, final_path: str, filename: str, kind: UploadKind
    ) -> None:
        with self.session_factory.begin() as session:
            upload_rows.insert_receiving(
                session,
                upload_id=upload_id,
                storage_path=final_path,
                original_filename=filename,
                kind=kind.value,
                content_type=DETECTED_CONTENT_TYPE[kind],
                # Computed with the database clock, like every other timestamp.
                expires_at=func.now() + self.ttl,
            )

    async def _stream_to_disk(
        self,
        upload_id: uuid.UUID,
        kind: UploadKind,
        limit: int,
        chunks: AsyncIterator[bytes],
    ) -> tuple[str, int]:
        handle = await to_thread.run_sync(self.storage.open_incoming, upload_id)
        hasher = hashlib.sha256()
        size = 0
        head = b""
        try:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > limit:  # counted on bytes actually received, not Content-Length
                    raise _RejectedError(UploadRejectionReason.TOO_LARGE, _too_large(kind, limit))
                if len(head) < SIGNATURE_LENGTH:
                    head += chunk[: SIGNATURE_LENGTH - len(head)]
                    if len(head) == SIGNATURE_LENGTH and not signature_matches(kind, head):
                        raise _signature_rejection(kind)
                await to_thread.run_sync(_write_chunk, handle, hasher, chunk)
            if size == 0:
                raise _RejectedError(
                    UploadRejectionReason.EMPTY_FILE,
                    ServiceError(ErrorCode.EMPTY_FILE, "The uploaded file is empty."),
                )
            if not signature_matches(kind, head):  # files shorter than SIGNATURE_LENGTH
                raise _signature_rejection(kind)
            await to_thread.run_sync(LocalStorage.finish_writing, handle)
        finally:
            if not handle.closed:
                handle.close()
        return hasher.hexdigest(), size

    def _mark_ready(self, upload_id: uuid.UUID, sha256: str, size: int) -> None:
        with self.session_factory.begin() as session:
            if not upload_rows.mark_ready(session, upload_id, sha256=sha256, size_bytes=size):
                raise RuntimeError(f"upload {upload_id} was no longer 'receiving'")

    async def _reject(
        self, upload_id: uuid.UUID, final_path: str, reason: UploadRejectionReason
    ) -> None:
        """Delete any files and mark the row rejected. Never raises; shielded from cancellation
        so cleanup still runs if the request is being cancelled."""
        with anyio.CancelScope(shield=True):
            try:
                await to_thread.run_sync(self._reject_sync, upload_id, final_path, reason)
            except Exception:
                logger.exception(
                    "could not clean up rejected upload", extra={"upload_id": str(upload_id)}
                )

    def _reject_sync(
        self, upload_id: uuid.UUID, final_path: str, reason: UploadRejectionReason
    ) -> None:
        # The temporary file is never referenced by a ready upload, so it can always go.
        self.storage.delete(self.storage.incoming_path(upload_id))
        # Mark the row first. It only changes 'receiving' rows: if the 'ready' update actually
        # committed (e.g. the connection dropped just after COMMIT, so we saw an error), the row
        # stays 'ready' and its file must be kept, or a usable-looking upload would have no file.
        with self.session_factory.begin() as session:
            marked = upload_rows.mark_rejected(session, upload_id, reason)
        if not marked:
            logger.warning(
                "upload failed after it was marked ready; keeping its file",
                extra={"upload_id": str(upload_id)},
            )
            return
        self.storage.delete(final_path)
        self.storage.remove_empty_parent(final_path)
        logger.info("upload rejected", extra={"upload_id": str(upload_id), "reason": reason.value})

    def _load(self, upload_id: uuid.UUID) -> Upload:
        with self.session_factory() as session:
            upload = session.get(Upload, upload_id)
            assert upload is not None
            return upload


def _write_chunk(handle: BinaryIO, hasher: "hashlib._Hash", chunk: bytes) -> None:
    hasher.update(chunk)
    handle.write(chunk)


def _too_large(kind: UploadKind, limit: int) -> ServiceError:
    return ServiceError(
        ErrorCode.UPLOAD_TOO_LARGE,
        f"The file exceeds the {_human_size(limit)} limit for {kind.value.upper()} uploads.",
        {"limit_bytes": limit},
    )


def _signature_rejection(kind: UploadKind) -> _RejectedError:
    return _RejectedError(
        UploadRejectionReason.SIGNATURE_MISMATCH,
        ServiceError(
            ErrorCode.FILE_SIGNATURE_MISMATCH,
            f"The file's contents are not a valid {kind.value.upper()} file "
            "(its signature doesn't match its extension).",
        ),
    )


def _human_size(size: int) -> str:
    for unit, factor in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size >= factor and size % factor == 0:
            return f"{size // factor} {unit}"
    return f"{size} bytes"
