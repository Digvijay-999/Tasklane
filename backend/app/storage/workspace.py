"""Per-job file area: intermediate stage outputs and final artifacts.

Layout (relative to the storage root, all names generated, never user-supplied):
    work/{job_id}/...        intermediate files written by stages (validate.json, pages.jsonl, ...)
    artifacts/{job_id}/...   final outputs offered for download (summary.md, manifest.json)

Every write is atomic: the data goes to a temporary file in the same folder, is flushed and
fsynced, then renamed over the final name. Readers therefore see either the old file or the
complete new one, never a partial file, and writing the same file twice simply replaces it.
"""

import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.storage.local import LocalStorage, fsync_directory


class JobWorkspace:
    def __init__(self, storage: LocalStorage, job_id: uuid.UUID) -> None:
        self.storage = storage
        self.job_id = job_id

    # --- Paths (relative to the storage root) -----------------------------------------------------

    def work(self, name: str) -> str:
        return f"work/{self.job_id}/{name}"

    def artifact(self, name: str) -> str:
        return f"artifacts/{self.job_id}/{name}"

    def absolute(self, relative: str) -> Path:
        return self.storage.absolute(relative)

    # --- Atomic writes ----------------------------------------------------------------------------

    def temp_path(self, relative: str) -> Path:
        """A fresh temporary path next to `relative` (same folder, so the rename is atomic)."""
        target = self.absolute(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")

    def commit(self, temp: Path, relative: str) -> None:
        """Atomically move a fully written temporary file into place."""
        target = self.absolute(relative)
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, target)
        fsync_directory(target.parent)

    def write_bytes(self, relative: str, data: bytes) -> None:
        temp = self.temp_path(relative)
        try:
            temp.write_bytes(data)
            self.commit(temp, relative)
        finally:
            temp.unlink(missing_ok=True)

    def write_text(self, relative: str, text: str) -> None:
        self.write_bytes(relative, text.encode("utf-8"))

    def write_json(self, relative: str, value: Any, *, ensure_ascii: bool = False) -> None:
        """Deterministic JSON (sorted keys), so the same data always gives the same bytes.
        ensure_ascii=True writes every non-ASCII character as a \\u escape (used for artifacts
        that contain untrusted names and text)."""
        self.write_text(
            relative,
            json.dumps(value, sort_keys=True, indent=2, ensure_ascii=ensure_ascii) + "\n",
        )

    def write_jsonl(
        self, relative: str, records: list[dict[str, Any]], *, ensure_ascii: bool = False
    ) -> None:
        lines = (json.dumps(r, sort_keys=True, ensure_ascii=ensure_ascii) + "\n" for r in records)
        self.write_text(relative, "".join(lines))

    def discard(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    # --- Reads ----------------------------------------------------------------------------------

    def exists(self, relative: str) -> bool:
        return self.storage.exists(relative)

    def read_text(self, relative: str) -> str:
        return self.absolute(relative).read_text(encoding="utf-8")

    def read_json(self, relative: str) -> Any:
        return json.loads(self.read_text(relative))

    def iter_jsonl(self, relative: str) -> Iterator[dict[str, Any]]:
        with self.absolute(relative).open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)

    def size(self, relative: str) -> int:
        return self.absolute(relative).stat().st_size

    def sha256(self, relative: str) -> str:
        return sha256_file(self.absolute(relative))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
