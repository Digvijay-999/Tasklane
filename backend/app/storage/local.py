"""Files on local disk, under one root directory (a named Docker volume in Compose).

Every path is generated from IDs; user-supplied names are never part of a path. Paths stored in
the database are *relative* to the root, so the root can move without rewriting rows.

Layout:
    uploads/.incoming/{upload_id}.part   while an upload is streaming in
    uploads/{upload_id}/source.{pdf|zip} after it completed (atomic rename)
"""

import os
import uuid
from pathlib import Path
from typing import BinaryIO

from app.domain.enums import UploadKind


class StoragePathError(ValueError):
    """A relative path tried to escape the storage root (a bug, never user input)."""


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    # --- Generated paths (relative to the root) ------------------------------------------------

    @staticmethod
    def upload_path(upload_id: uuid.UUID, kind: UploadKind) -> str:
        return f"uploads/{upload_id}/source.{kind.value}"

    @staticmethod
    def incoming_path(upload_id: uuid.UUID) -> str:
        return f"uploads/.incoming/{upload_id}.part"

    def absolute(self, relative: str) -> Path:
        """Resolve a stored relative path, refusing anything outside the root."""
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise StoragePathError(f"path escapes storage root: {relative!r}")
        return path

    # --- Writing ---------------------------------------------------------------------------------

    def open_incoming(self, upload_id: uuid.UUID) -> BinaryIO:
        """Create the temporary file for an upload. Fails if it already exists ("x" mode)."""
        path = self.absolute(self.incoming_path(upload_id))
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("xb")

    @staticmethod
    def finish_writing(handle: BinaryIO) -> None:
        """Flush and fsync, so the bytes are on disk before the file is renamed into place."""
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()

    def commit_incoming(self, upload_id: uuid.UUID, final_relative: str) -> None:
        """Move a completed upload from its temporary path to its final path.

        os.replace is atomic on the same filesystem: other code sees either no file or the whole
        file at the final path, never a partial one.
        """
        source = self.absolute(self.incoming_path(upload_id))
        target = self.absolute(final_relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        # Make the rename durable: fsync the directory holding the file, and its parent, which
        # holds the newly created per-upload directory's entry.
        _fsync_directory(target.parent)
        _fsync_directory(target.parent.parent)

    def delete(self, relative: str) -> None:
        """Delete a file if it exists (used to clean up rejected or interrupted uploads)."""
        self.absolute(relative).unlink(missing_ok=True)

    def remove_empty_parent(self, relative: str) -> None:
        """Remove the directory containing `relative` if it is empty (best effort)."""
        parent = self.absolute(relative).parent
        if parent == self.root:
            return
        try:
            parent.rmdir()
        except OSError:
            pass  # not empty, or already gone

    def exists(self, relative: str) -> bool:
        return self.absolute(relative).is_file()


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # not supported on some platforms (e.g. Windows); best effort
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
