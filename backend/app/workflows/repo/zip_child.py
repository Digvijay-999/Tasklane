"""The ZIP reader that runs in a separate, limited process. Started ONLY by
app/workflows/repo/archive.py, through app/workflows/sandbox.py:

    python -I limited_launcher.py <limits...> zip_child.py inspect <zip> <output.json> <root>
        <max_entries> <max_path_length>
    python -I limited_launcher.py <limits...> zip_child.py extract <zip> <request.json>
        <blob output> <index output>

Why a separate process: the archive comes from an untrusted upload. A crafted file can make the
reader loop, eat memory, or expand to gigabytes. Here the parent kills this process at a
deadline, and the operating system caps its memory, CPU time and output file size.

What this file never does: create a file or folder named after an archive entry, follow or
create a symlink, or execute, import or build anything from the archive. `extract` copies the
bytes of the requested entries into ONE output file ("blob") at offsets this code chooses, so a
name like "../../etc/passwd" can't reach the filesystem at all. Names are still checked (and the
whole archive rejected for any unsafe one), because they appear in the outputs and because other
tools would trust them.

Standard library only, and no application code (a test enforces it): `python -I` makes the app
unimportable here anyway, and the parent passes no secrets in the environment.

Protocol: one JSON object per line on stdout (see app/workflows/sandbox.py).
"""

import errno
import json
import os
import re
import stat
import sys
import unicodedata
import zipfile

BLOCK = 64 * 1024
_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")
_ENCRYPTED_FLAG = 0x1
_AES_METHOD = 99  # WinZip AES: the "compression method" of AES-encrypted entries
MAX_NAME_CHARS = 4096  # longer than any real path (Linux PATH_MAX): never a legitimate name
# Trouble with the host's disk, not with the archive: reported as the transient
# storage_unavailable (like errors.py classifies them in the worker).
STORAGE_ERRNOS = frozenset({errno.ENOSPC, errno.EIO, errno.EROFS, errno.EDQUOT, errno.EAGAIN})


def emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def fail(error_class: str, **params: int) -> None:
    emit({"type": "error", "error_class": error_class, "params": params})
    sys.exit(0)


# --- Names ----------------------------------------------------------------------------------------


def unsafe_name(name: str, root: str, *, resolve: bool = True) -> bool:
    """True if an entry name could escape the extraction folder or be misread by other tools.
    `root` is a real, resolved folder; the final check resolves the name against it, so it holds
    even if a string pattern above were missed. (resolve=False skips that check for names that
    are too long to be used anywhere: they are skipped as path_too_long.)"""
    if not name or len(name) > MAX_NAME_CHARS or "\x00" in name or "\\" in name:
        return True
    if any(ord(char) < 32 or ord(char) == 127 for char in name):  # control characters
        return True
    if name.startswith("/") or _DRIVE_LETTER.match(name):  # absolute, or C:...
        return True
    body = name[:-1] if name.endswith("/") else name  # a folder entry ends with one "/"
    if any(part in ("", ".", "..") for part in body.split("/")):
        return True
    if not resolve:
        return False
    target = os.path.realpath(os.path.join(root, body))
    return os.path.commonpath([root, target]) != root


def collision_key(name: str) -> str:
    """Names that differ only in letter case or Unicode form are the same file on Windows and
    macOS, so they count as duplicates."""
    return unicodedata.normalize("NFC", name.rstrip("/")).casefold()


def entry_kind(info: zipfile.ZipInfo) -> str:
    """file, dir, symlink or special, from the Unix mode bits stored in the entry."""
    file_type = stat.S_IFMT(info.external_attr >> 16)
    if file_type == stat.S_IFLNK:
        return "symlink"
    if info.is_dir() or file_type == stat.S_IFDIR:
        return "dir"
    if file_type not in (0, stat.S_IFREG):  # devices, pipes, sockets
        return "special"
    return "file"


# --- inspect: the central directory only; nothing is decompressed ---------------------------------


def open_archive(path: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except MemoryError:
        raise
    except OSError as exc:
        if exc.errno in STORAGE_ERRNOS:
            raise
        fail("zip_corrupt")
    except Exception:  # BadZipFile, a name that isn't valid UTF-8, truncated file, ...
        fail("zip_corrupt")
        raise  # unreachable: fail() exits


def inspect(path: str, output: str, root: str, max_entries: int, max_path_length: int) -> None:
    archive = open_archive(path)
    infos = archive.infolist()
    if len(infos) > max_entries:
        fail("zip_too_many_files", entries=len(infos), limit=max_entries)
    root = os.path.realpath(root)
    seen: set[str] = set()
    folders: set[str] = set()  # every folder implied by a path ("a/b/c.py" implies a, a/b)
    files: set[str] = set()
    entries = []
    for index, info in enumerate(infos):
        # orig_filename keeps a NUL byte that zipfile cuts off in .filename; .filename may come
        # from a Unicode-path extra field. Both must be safe.
        too_long = len(info.filename) > max_path_length
        for name in (info.orig_filename, info.filename):
            if unsafe_name(name, root, resolve=not too_long):
                fail("zip_unsafe_path")
        if info.flag_bits & _ENCRYPTED_FLAG or info.compress_type == _AES_METHOD:
            fail("zip_encrypted")
        name = info.filename
        key = collision_key(name)
        if key in seen:  # exact duplicate, or differs only in case/Unicode form
            fail("zip_unsafe_path")
        seen.add(key)
        kind = entry_kind(info)
        if not too_long:  # a too-long name is skipped anyway; its prefixes could be huge
            parts = key.split("/")
            folders.update("/".join(parts[:depth]) for depth in range(1, len(parts)))
            if kind != "dir":
                files.add(key)
        entries.append(
            {
                "index": index,
                # A very long name is cut here (and the entry skipped as path_too_long), so the
                # output stays small; the checks above used the full name.
                "path": name[:max_path_length] if too_long else name,
                "path_too_long": too_long,
                "kind": kind,
                "size": info.file_size,  # declared: used only to fail fast or skip early
                "compressed_size": info.compress_size,
            }
        )
    if files & folders:  # "a" is both a file and a folder: ambiguous, rejected
        fail("zip_unsafe_path")
    with open(output, "w", encoding="utf-8") as handle:
        json.dump({"entries": entries}, handle)
    emit({"type": "result", "entries": len(entries)})


# --- extract: real bytes, counted while decompressing ---------------------------------------------


class Budget:
    """Counts real decompressed bytes and enforces the size and ratio limits. Sizes declared in
    the ZIP headers are never trusted for this."""

    def __init__(self, max_total: int, max_ratio: int, ratio_min_bytes: int) -> None:
        self.max_total = max_total
        self.max_ratio = max_ratio
        self.ratio_min_bytes = ratio_min_bytes
        self.total_out = 0
        self.total_in = 0  # compressed bytes of the entries read so far

    def start_entry(self, compressed_size: int) -> None:
        self.total_in += compressed_size

    def add(self, entry_out: int, compressed_size: int, produced: int) -> None:
        self.total_out += produced
        if self.total_out > self.max_total:
            fail("zip_too_large", limit=self.max_total)
        if self.over_ratio(entry_out, compressed_size) or self.over_ratio(
            self.total_out, self.total_in
        ):
            fail("zip_bomb", ratio=self.max_ratio)

    def over_ratio(self, out: int, compressed: int) -> bool:
        # Below ratio_min_bytes the expansion is harmless (small, very repetitive text files
        # compress extremely well); above it, a high ratio means a bomb.
        return out > self.ratio_min_bytes and out > self.max_ratio * max(compressed, 1)


def read_entry(stream, max_file_bytes: int, on_bytes) -> bytes | None:  # type: ignore[no-untyped-def]
    """Read one entry, counting real bytes. Returns None as soon as it is bigger than
    max_file_bytes (the rest is never decompressed)."""
    data = bytearray()
    while True:
        block = stream.read(BLOCK)
        if not block:
            return bytes(data)
        data += block
        on_bytes(len(data), len(block))
        if len(data) > max_file_bytes:
            return None


def extract(path: str, request_path: str, blob_path: str, index_path: str) -> None:
    with open(request_path, encoding="utf-8") as handle:
        request = json.load(handle)
    max_file = int(request["max_file_bytes"])
    budget = Budget(
        int(request["max_total_bytes"]),
        int(request["max_ratio"]),
        int(request["ratio_min_bytes"]),
    )
    archive = open_archive(path)
    infos = archive.infolist()
    wanted = [(int(i), infos[int(i)]) for i in request["indexes"]]

    # Fail fast on the declared sizes when they already show a problem. They can lie, so the
    # real byte counts below are what actually enforce the limits.
    if sum(info.file_size for _, info in wanted) > budget.max_total:
        fail("zip_too_large", limit=budget.max_total)
    if any(budget.over_ratio(info.file_size, info.compress_size) for _, info in wanted):
        fail("zip_bomb", ratio=budget.max_ratio)

    results = []
    offset = 0
    with open(blob_path, "wb") as blob:
        for done, (index, info) in enumerate(wanted, start=1):
            budget.start_entry(info.compress_size)
            try:
                with archive.open(info) as stream:
                    data = read_entry(
                        stream,
                        max_file,
                        lambda entry_out, produced, info=info: budget.add(
                            entry_out, info.compress_size, produced
                        ),
                    )
            except (MemoryError, SystemExit):
                raise
            except OSError as exc:
                if exc.errno == errno.EFBIG or exc.errno in STORAGE_ERRNOS:
                    raise  # handled in main(): the limits or the host's disk, not the archive
                fail("zip_corrupt")
            except Exception:  # bad CRC, damaged data, unsupported method, mismatched header
                fail("zip_corrupt")
            if data is None:
                results.append({"index": index, "too_large": True})
            else:
                blob.write(data)
                results.append({"index": index, "offset": offset, "size": len(data)})
                offset += len(data)
            emit({"type": "progress", "done": done, "total": len(wanted)})
    with open(index_path, "w", encoding="utf-8") as handle:
        json.dump({"files": results, "blob_size": offset}, handle)
    emit({"type": "result", "files": len(results), "bytes": budget.total_out})


def main(argv: list[str]) -> None:
    mode, args = argv[0], argv[1:]
    try:
        if mode == "inspect":
            path, output, root, max_entries, max_path_length = args
            inspect(path, output, root, int(max_entries), int(max_path_length))
        elif mode == "extract":
            extract(*args)
        else:
            raise SystemExit(f"unknown mode {mode!r}")
    except OSError as exc:
        if exc.errno == errno.EFBIG:  # over the output-size limit set by the parent
            fail("zip_too_complex")
        if exc.errno in STORAGE_ERRNOS:
            fail("storage_unavailable")
        raise


if __name__ == "__main__":
    main(sys.argv[1:])
