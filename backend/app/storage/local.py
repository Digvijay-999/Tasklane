"""Files on local disk, under one root directory (a named Docker volume in Compose).

Every path is generated from IDs; user-supplied names are never part of a path. Paths stored in
the database are *relative* to the root, so the root can move without rewriting rows.

Layout:
    uploads/.incoming/{upload_id}.part   while an upload is streaming in
    uploads/{upload_id}/source.{pdf|zip} after it completed (atomic rename)
"""

import os
import shutil
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
        fsync_directory(target.parent)
        fsync_directory(target.parent.parent)

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

    # --- Cleanup (Phase 12) ---------------------------------------------------------------------

    def checked(self, relative: str) -> Path:
        """Like absolute(), but also refuses a path that resolves through a symlink (or is the
        root itself): cleanup deletes exactly the path it named, never where a link points."""
        path = self.absolute(relative)
        if path == self.root or path != self.root.joinpath(relative):
            raise StoragePathError(f"refusing to clean up {relative!r}")
        return path

    def list_dir(self, relative: str) -> list[str]:
        """Entry names in a folder under the root ([] if it doesn't exist)."""
        try:
            return sorted(os.listdir(self.checked(relative)))
        except FileNotFoundError:
            return []

    def files_under(self, relative: str) -> list[str]:
        """Every file (and symlink, which is never followed) below a folder, as relative paths."""
        top = self.checked(relative)
        found = []
        for folder, dirs, files in os.walk(top):
            links = [name for name in dirs if os.path.islink(os.path.join(folder, name))]
            for name in [*files, *links]:
                found.append(Path(folder, name).relative_to(self.root).as_posix())
        return sorted(found)

    def size_of(self, relative: str) -> int:
        """Bytes in a file, or in every file below a folder (0 if missing)."""
        path = self.root.joinpath(relative)
        if path.is_dir() and not path.is_symlink():
            return sum(_size(self.root.joinpath(name)) for name in self.files_under(relative))
        self.checked(Path(relative).parent.as_posix())  # the folder must be a real one
        return _size(path)

    def remove_file(self, relative: str) -> int | None:
        """Delete one file (a symlink itself, never its target). Returns the bytes freed, or
        None if it was already gone."""
        path = self.root.joinpath(relative)
        self.checked(Path(relative).parent.as_posix())  # the folder must be a real one
        try:
            size = path.lstat().st_size
            path.unlink()
        except FileNotFoundError:
            return None
        return size

    def remove_tree(self, relative: str) -> int | None:
        """Delete a folder and everything in it. Returns the bytes freed, or None if it was
        already gone."""
        path = self.checked(relative)
        if not path.exists():
            return None
        size = self.size_of(relative)
        # Never follows symlinks inside the tree; entries a concurrent cleanup removed first
        # are fine.
        shutil.rmtree(path, onexc=_ignore_missing)
        return size

    def remove_empty_dirs(self, relative: str) -> None:
        """Remove empty folders below `relative`, and `relative` itself if it ends up empty."""
        top = self.checked(relative)
        for folder, _dirs, _files in os.walk(top, topdown=False):
            try:
                os.rmdir(folder)
            except OSError:
                pass  # not empty


def _size(path: Path) -> int:
    try:
        return path.lstat().st_size
    except FileNotFoundError:
        return 0


def _ignore_missing(_function: object, _path: str, error: BaseException) -> None:
    if not isinstance(error, FileNotFoundError):
        raise error


def fsync_directory(path: Path) -> None:
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
