"""The contract between the worker and stage code.

Stages never touch the database, Celery, or HTTP. They read and write files through the job's
workspace, and report everything else (progress, provider calls, artifacts) through the
StageContext; the execution service turns those reports into database writes, each one fenced
by the attempt's lease.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.providers.base import ProviderCallRecord
from app.storage.workspace import JobWorkspace


@dataclass(frozen=True)
class UploadInput:
    upload_id: uuid.UUID
    storage_path: str  # relative to the storage root
    sha256: str
    size_bytes: int
    original_filename: str  # display only


@dataclass(frozen=True)
class JobInput:
    job_id: uuid.UUID
    attempt_no: int
    workflow_type: str
    upload: UploadInput
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactOutput:
    """A final output file to register for download."""

    artifact_type: str
    relative_path: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str


class StageContext(Protocol):
    """What a running stage can ask of the worker."""

    job: JobInput
    workspace: JobWorkspace

    def report_progress(self, percent: int) -> None:
        """Record progress (0-100) within the current stage and renew the lease.

        If the worker no longer owns the job (its attempt was closed by someone else), this
        raises, which stops the stage; stages must not catch it.
        """
        ...

    def keep_alive(self) -> None:
        """Renew the lease without recording anything (for long waits). Raises like
        report_progress if the worker no longer owns the job."""
        ...

    def record_provider_call(self, call: ProviderCallRecord) -> None:
        """Store one summarization provider call (also renews the lease)."""
        ...

    def save_artifact(self, artifact: ArtifactOutput) -> None:
        """Register (or replace) a final artifact of this job (also renews the lease). The file
        must already be written; the artifact becomes visible only if the job succeeds."""
        ...


class Stage(Protocol):
    name: str

    def run(self, context: StageContext) -> None:
        """Do the stage's work. Raise to fail the attempt (a JobError for expected failures)."""
        ...
