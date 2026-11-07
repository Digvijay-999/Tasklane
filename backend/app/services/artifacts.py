"""Looking up artifact files for download."""

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.db.repositories.artifacts import get_artifact
from app.services.errors import ErrorCode, ServiceError
from app.storage.local import LocalStorage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArtifactFile:
    path: Path  # absolute, always inside the storage root
    filename: str
    content_type: str
    size_bytes: int
    sha256: str


class ArtifactService:
    def __init__(self, session_factory: sessionmaker[Session], storage: LocalStorage) -> None:
        self.session_factory = session_factory
        self.storage = storage

    def open(self, job_id: uuid.UUID, artifact_id: uuid.UUID) -> ArtifactFile:
        with self.session_factory() as session:
            artifact = get_artifact(session, job_id, artifact_id)
        if artifact is None:  # unknown, or belongs to another job
            raise ServiceError(
                ErrorCode.ARTIFACT_NOT_FOUND, "No artifact with this id for this job."
            )
        # The path comes from the database (never from the request) and is checked against the
        # storage root.
        path = self.storage.absolute(artifact.storage_path)
        if not path.is_file() or path.stat().st_size != artifact.size_bytes:
            logger.warning(
                "artifact file missing or changed",
                extra={"job_id": str(job_id), "artifact_id": str(artifact_id)},
            )
            raise ServiceError(
                ErrorCode.ARTIFACT_GONE, "The artifact's file is no longer available."
            )
        return ArtifactFile(
            path=path,
            filename=artifact.filename,
            content_type=artifact.content_type,
            size_bytes=artifact.size_bytes,
            sha256=artifact.sha256,
        )
