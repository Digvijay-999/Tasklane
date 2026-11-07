"""Run the ZIP child (zip_child.py) in the shared sandbox and check what it returns.

The child is our own code, but it has just read hostile input, so its output files are checked
here before anything uses them: types, ranges, the requested entries only, offsets that tile the
blob exactly. A mismatch is a bug, never the user's fault: it raises RuntimeError
(internal_error).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.workflows.sandbox import SandboxLimits, run_sandboxed

CHILD_SCRIPT = Path(__file__).with_name("zip_child.py")
ENTRY_KINDS = frozenset({"file", "dir", "symlink", "special"})
# The only error classes the ZIP child may report (anything else becomes internal_error).
ZIP_CHILD_ERRORS = frozenset(
    {
        "zip_corrupt",
        "zip_encrypted",
        "zip_unsafe_path",
        "zip_too_many_files",
        "zip_too_large",
        "zip_bomb",
        "zip_too_complex",
        "storage_unavailable",  # the host's disk failed (transient, retried)
    }
)
INSPECT_OUTPUT_MIN_BYTES = 64 * 1024 * 1024
INDEX_MARGIN_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ZipLimits:
    max_entries: int
    max_total_bytes: int
    max_file_bytes: int
    max_compression_ratio: int
    ratio_check_min_bytes: int
    max_path_length: int
    max_path_depth: int
    memory_bytes: int

    def to_record(self) -> dict[str, int]:
        """For the manifest (memory is an implementation detail, not a rule about the input)."""
        return {
            "max_entries": self.max_entries,
            "max_total_bytes": self.max_total_bytes,
            "max_file_bytes": self.max_file_bytes,
            "max_compression_ratio": self.max_compression_ratio,
            "ratio_check_min_bytes": self.ratio_check_min_bytes,
            "max_path_length": self.max_path_length,
            "max_path_depth": self.max_path_depth,
        }


@dataclass(frozen=True)
class SandboxRun:
    timeout_seconds: float
    keep_alive: Callable[[], None]
    keep_alive_interval: float
    on_progress: Callable[[int, int], None] | None = None


def inspect_archive(
    zip_path: Path, output: Path, root: Path, limits: ZipLimits, run: SandboxRun
) -> list[dict[str, Any]]:
    """Read the central directory (nothing is decompressed). Raises JobError for an archive that
    is corrupt, encrypted, has too many entries or any unsafe name. Returns the entries."""
    _run(
        "inspect",
        [
            str(zip_path),
            str(output),
            str(root),
            str(limits.max_entries),
            str(limits.max_path_length),
        ],
        SandboxLimits(limits.memory_bytes, inspect_output_max_bytes(limits)),
        run,
    )
    document = json.loads(output.read_text(encoding="utf-8"))
    entries = document["entries"] if isinstance(document, dict) else None
    if not isinstance(entries, list) or len(entries) > limits.max_entries:
        raise RuntimeError("zip child returned a malformed entry list")
    checked = []
    for expected_index, entry in enumerate(entries):
        record = {
            "index": entry["index"],
            "path": entry["path"],
            "path_too_long": entry["path_too_long"],
            "kind": entry["kind"],
            "size": entry["size"],
            "compressed_size": entry["compressed_size"],
        }
        valid = (
            record["index"] == expected_index
            and isinstance(record["path"], str)
            and 0 < len(record["path"]) <= limits.max_path_length
            and isinstance(record["path_too_long"], bool)
            and record["kind"] in ENTRY_KINDS
            and all(_non_negative_int(record[k]) for k in ("size", "compressed_size"))
        )
        if not valid:
            raise RuntimeError("zip child returned a malformed entry")
        checked.append(record)
    return checked


def inspect_output_max_bytes(limits: ZipLimits) -> int:
    """Room for archive.json at the worst case: every entry with a name at the length limit, all
    of it written as 12-byte JSON escapes (a surrogate pair), plus the other fields."""
    worst = limits.max_entries * (limits.max_path_length * 12 + 256)
    return max(INSPECT_OUTPUT_MIN_BYTES, worst)


def extract_entries(
    zip_path: Path,
    indexes: list[int],
    *,
    request: Path,
    blob: Path,
    index_file: Path,
    limits: ZipLimits,
    run: SandboxRun,
) -> dict[int, dict[str, int] | None]:
    """Copy the requested entries' bytes into `blob`. Returns index -> {"offset", "size"}, or
    None for an entry that turned out bigger than max_file_bytes (not copied). Raises JobError
    for a corrupt archive, a bomb, or contents over the total limit."""
    request.write_text(
        json.dumps(
            {
                "indexes": indexes,
                "max_file_bytes": limits.max_file_bytes,
                "max_total_bytes": limits.max_total_bytes,
                "max_ratio": limits.max_compression_ratio,
                "ratio_min_bytes": limits.ratio_check_min_bytes,
            }
        ),
        encoding="utf-8",
    )
    _run(
        "extract",
        [str(zip_path), str(request), str(blob), str(index_file)],
        SandboxLimits(limits.memory_bytes, limits.max_total_bytes + INDEX_MARGIN_BYTES),
        run,
    )
    document = json.loads(index_file.read_text(encoding="utf-8"))
    files = document.get("files") if isinstance(document, dict) else None
    if not isinstance(files, list) or [f.get("index") for f in files] != indexes:
        raise RuntimeError("zip child did not return exactly the requested entries")
    results: dict[int, dict[str, int] | None] = {}
    offset = 0
    for item in files:
        if item.get("too_large") is True:
            results[item["index"]] = None
            continue
        size = item.get("size")
        if item.get("offset") != offset or not _non_negative_int(size):
            raise RuntimeError("zip child returned inconsistent offsets")
        if size > limits.max_file_bytes:
            raise RuntimeError("zip child returned a file over the size limit")
        results[item["index"]] = {"offset": offset, "size": size}
        offset += size
    if document.get("blob_size") != offset or blob.stat().st_size != offset:
        raise RuntimeError("zip child's blob doesn't match its index")
    return results


def _run(mode: str, args: list[str], limits: SandboxLimits, run: SandboxRun) -> dict[str, Any]:
    return run_sandboxed(
        CHILD_SCRIPT,
        [mode, *args],
        limits=limits,
        timeout_seconds=run.timeout_seconds,
        keep_alive=run.keep_alive,
        keep_alive_interval=run.keep_alive_interval,
        too_complex_error="zip_too_complex",
        allowed_errors=ZIP_CHILD_ERRORS,
        on_progress=run.on_progress,
    )


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
