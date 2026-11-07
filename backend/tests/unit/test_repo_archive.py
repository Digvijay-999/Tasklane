"""The sandboxed ZIP reader (zip_child.py via archive.py): real child processes, real limits.

Covers acceptance #8 at the lowest level: every malicious archive is rejected with the right
error class (or the entry skipped), before anything is written by name.
"""

import os
import stat
from pathlib import Path

import pytest

from app.domain.errors import ERRORS, JobError
from app.workflows.repo import zip_child
from app.workflows.repo.archive import SandboxRun, ZipLimits, extract_entries, inspect_archive
from devtools import zip_samples

KIB = 1024
LIMITS = ZipLimits(
    max_entries=50,
    max_total_bytes=512 * KIB,
    max_file_bytes=128 * KIB,
    max_compression_ratio=100,
    ratio_check_min_bytes=16 * KIB,
    max_path_length=240,
    max_path_depth=20,
    memory_bytes=512 * KIB * KIB,
)
RUN = SandboxRun(timeout_seconds=30, keep_alive=lambda: None, keep_alive_interval=5)


def inspect(tmp_path: Path, data: bytes, limits: ZipLimits = LIMITS) -> list[dict]:
    source = tmp_path / "upload.zip"
    source.write_bytes(data)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return inspect_archive(source, work / "archive.json", work, limits, RUN)


def extract(tmp_path: Path, data: bytes, limits: ZipLimits = LIMITS):
    """Inspect, then extract every regular file. Returns (entries, results, blob bytes)."""
    entries = inspect(tmp_path, data, limits)
    indexes = [e["index"] for e in entries if e["kind"] == "file"]
    work = tmp_path / "work"
    results = extract_entries(
        tmp_path / "upload.zip",
        indexes,
        request=work / "request.json",
        blob=work / "files.bin",
        index_file=work / "index.json",
        limits=limits,
        run=RUN,
    )
    return entries, results, (work / "files.bin").read_bytes()


def error_of(call, *args, **kwargs) -> JobError:
    with pytest.raises(JobError) as error:
        call(*args, **kwargs)
    return error.value


# --- Whole-archive rejections (acceptance #8) -----------------------------------------------------


def _nul_name_zip() -> bytes:
    # zipfile can't write a NUL in a name, so write a placeholder and patch the bytes.
    data = zip_samples.small_repo([("evilXnul.txt", b"x")])
    return data.replace(b"evilXnul.txt", b"evil\x00nul.txt")


UNSAFE = {
    "zip slip": zip_samples.zipslip_zip(),
    "absolute path": zip_samples.small_repo([("/etc/evil.txt", b"x")]),
    "drive letter": zip_samples.small_repo([("C:/Windows/evil.txt", b"x")]),
    "drive letter, relative": zip_samples.small_repo([("C:evil.txt", b"x")]),
    "backslash traversal": zip_samples.small_repo([("..\\..\\evil.txt", b"x")]),
    "backslash inside": zip_samples.small_repo([("src\\..\\..\\evil.txt", b"x")]),
    "dot-dot in the middle": zip_samples.small_repo([("src/../../evil.txt", b"x")]),
    "empty segment": zip_samples.small_repo([("src//evil.txt", b"x")]),
    "dot segment": zip_samples.small_repo([("./src/evil.txt", b"x")]),
    "NUL byte": _nul_name_zip(),
    "control character": zip_samples.small_repo([("src/evil\n.txt", b"x")]),
    "duplicate names": zip_samples.small_repo([("src/shop/orders.py", b"second copy")]),
    "case collision": zip_samples.small_repo([("readme.MD", b"x")]),
    "file and folder with one name": zip_samples.small_repo([("README.md/inner.py", b"x")]),
}


@pytest.mark.parametrize("data", UNSAFE.values(), ids=UNSAFE.keys())
def test_unsafe_names_reject_the_whole_archive(tmp_path, data):
    error = error_of(inspect, tmp_path, data)
    assert error.error_class == "zip_unsafe_path"
    assert error.safe_message == ERRORS["zip_unsafe_path"].message  # no names in the message
    assert sorted(p.name for p in (tmp_path / "work").iterdir()) == []  # nothing written


@pytest.mark.parametrize(
    ("data", "error_class"),
    [
        (zip_samples.encrypted_zip(), "zip_encrypted"),
        (zip_samples.corrupt_zip(), "zip_corrupt"),
        (b"PK\x03\x04" + b"\x00garbage" * 40, "zip_corrupt"),
        (b"", "zip_corrupt"),
    ],
    ids=["encrypted", "truncated", "garbage after signature", "empty file"],
)
def test_encrypted_or_corrupt_archives_are_rejected(tmp_path, data, error_class):
    assert error_of(inspect, tmp_path, data).error_class == error_class


def test_too_many_entries(tmp_path):
    data = zip_samples.build_zip([(f"src/f{i}.py", b"x = 1\n") for i in range(51)])
    error = error_of(inspect, tmp_path, data)
    assert error.error_class == "zip_too_many_files"
    assert error.safe_message == "The archive has 51 entries; the limit is 50."


# --- Entries that are skipped, not extracted ------------------------------------------------------


def test_symlinks_and_special_files_are_detected_from_their_mode_bits(tmp_path):
    entries = inspect(
        tmp_path,
        zip_samples.small_repo(
            [
                ("link.py", b"/etc/passwd", stat.S_IFLNK | 0o777),
                ("pipe.py", b"", stat.S_IFIFO | 0o644),
                ("folder/", b"", stat.S_IFDIR | 0o755),
            ]
        ),
    )
    kinds = {e["path"]: e["kind"] for e in entries}
    assert kinds == {
        "README.md": "file",
        "src/shop/orders.py": "file",
        "link.py": "symlink",
        "pipe.py": "special",
        "folder/": "dir",
    }


def test_a_very_long_name_is_cut_and_flagged(tmp_path):
    name = "a/" + "n" * 300 + ".py"
    [*_, entry] = inspect(tmp_path, zip_samples.small_repo([(name, b"x")]))
    assert entry["path_too_long"] is True and entry["path"] == name[:240]


# --- Extraction: real bytes, bombs, lies ----------------------------------------------------------


def test_extraction_copies_only_the_requested_entries_into_one_blob(tmp_path):
    entries, results, blob = extract(tmp_path, zip_samples.small_repo())
    assert [e["path"] for e in entries] == ["README.md", "src/shop/orders.py"]
    readme = results[0]
    assert blob[readme["offset"] : readme["offset"] + readme["size"]].startswith(b"# Sample shop")
    # Nothing in the work folder is named after an entry.
    assert sorted(p.name for p in (tmp_path / "work").iterdir()) == [
        "archive.json",
        "files.bin",
        "index.json",
        "request.json",
    ]


def test_a_high_ratio_bomb_is_rejected(tmp_path):
    data = zip_samples.bomb_zip(size=100 * KIB)  # ~1000:1, over the 16 KiB floor
    error = error_of(extract, tmp_path, data)
    assert error.error_class == "zip_bomb"
    assert error.safe_message == ERRORS["zip_bomb"].message.format(ratio=100)


def test_small_repetitive_files_are_not_bombs(tmp_path):
    """Below ratio_check_min_bytes a high ratio is harmless (e.g. a file of blank lines)."""
    _, results, _ = extract(tmp_path, zip_samples.small_repo([("blank.txt", b"\n" * 8 * KIB)]))
    assert results[2]["size"] == 8 * KIB


def test_contents_over_the_total_limit_are_rejected(tmp_path):
    files = [(f"src/f{i}.txt", os.urandom(100 * KIB)) for i in range(6)]  # 600 KiB > 512 KiB
    error = error_of(extract, tmp_path, zip_samples.build_zip(files))
    assert error.error_class == "zip_too_large"


def test_a_header_that_understates_the_size_is_caught(tmp_path):
    """The entry really holds 200 KiB but claims 1 KiB: the reader stops at the claimed size and
    the checksum doesn't match, so the archive is rejected without trusting either number."""
    data = zip_samples.small_repo([("src/big.txt", b"lorem ipsum " * 17_000)])
    lying = zip_samples.set_declared_size(data, 2, KIB)
    error = error_of(extract, tmp_path, lying)
    assert error.error_class == "zip_corrupt"
    assert not (tmp_path / "work" / "files.bin").exists() or (
        (tmp_path / "work" / "files.bin").stat().st_size < 64 * KIB
    )


def test_a_header_that_overstates_the_size_fails_fast(tmp_path):
    data = zip_samples.small_repo([("src/a.txt", b"tiny")])
    lying = zip_samples.set_declared_size(data, 2, 400 * KIB)
    # 400 KiB claimed from a few compressed bytes: over the ratio limit before decompressing.
    assert error_of(extract, tmp_path, lying).error_class == "zip_bomb"


def test_an_entry_bigger_than_the_file_limit_is_skipped_not_copied(tmp_path):
    big = os.urandom(150 * KIB)  # incompressible, so not a bomb
    _, results, blob = extract(tmp_path, zip_samples.small_repo([("src/big.txt", big)]))
    assert results[2] is None
    assert big[:1024] not in blob


# --- The byte counter itself (zip_child.Budget / read_entry), with a lying stream -----------------


class EndlessStream:
    """Decompresses to far more than any header said."""

    def read(self, size):
        return b"\x00" * size


def test_the_counter_stops_a_file_at_the_limit_whatever_the_header_says(monkeypatch):
    budget = zip_child.Budget(max_total=10**9, max_ratio=10**9, ratio_min_bytes=10**9)
    data = zip_child.read_entry(EndlessStream(), 200 * KIB, lambda out, n: budget.add(out, 1000, n))
    assert data is None
    assert budget.total_out <= 200 * KIB + zip_child.BLOCK  # stopped right after the limit


@pytest.mark.parametrize(
    ("budget", "error_class"),
    [
        (
            zip_child.Budget(max_total=300 * KIB, max_ratio=10**9, ratio_min_bytes=0),
            "zip_too_large",
        ),
        (zip_child.Budget(max_total=10**9, max_ratio=100, ratio_min_bytes=16 * KIB), "zip_bomb"),
    ],
    ids=["total", "ratio"],
)
def test_the_counter_enforces_total_and_ratio_on_real_bytes(budget, error_class, capsys):
    budget.start_entry(1000)
    with pytest.raises(SystemExit):
        zip_child.read_entry(EndlessStream(), 10**9, lambda out, n: budget.add(out, 1000, n))
    assert f'"error_class": "{error_class}"' in capsys.readouterr().out


# --- Names are checked by resolving them, not only by patterns ------------------------------------


def test_names_are_resolved_against_the_real_folder(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    os.symlink("/etc", root / "link")  # a symlink planted inside the folder
    real_root = os.path.realpath(root)
    assert not zip_child.unsafe_name("src/main.py", real_root)
    # The name itself looks harmless, but it resolves outside the folder.
    assert zip_child.unsafe_name("link/passwd", real_root)


def test_names_that_differ_only_in_unicode_form_collide(tmp_path):
    composed, decomposed = "caf" + chr(0xE9) + ".py", "cafe" + chr(0x301) + ".py"
    data = zip_samples.small_repo([(composed, b"a = 1"), (decomposed, b"b = 2")])
    assert error_of(inspect, tmp_path, data).error_class == "zip_unsafe_path"


def test_a_name_longer_than_any_real_path_is_unsafe(tmp_path):
    data = zip_samples.small_repo([("a" * 5000 + ".py", b"x")])
    assert error_of(inspect, tmp_path, data).error_class == "zip_unsafe_path"


def test_a_very_deep_long_name_is_cheap_to_inspect_and_flagged_too_long(tmp_path):
    name = "node_modules/" + "d/" * 2000 + "x.py"  # 4017 characters, 2002 components
    [*_, entry] = inspect(tmp_path, zip_samples.small_repo([(name, b"x")]))
    assert entry["path_too_long"] is True


def test_a_reader_that_runs_past_the_deadline_is_too_complex(tmp_path):
    source = tmp_path / "upload.zip"
    source.write_bytes(zip_samples.small_repo())
    (tmp_path / "work").mkdir()
    hurried = SandboxRun(timeout_seconds=0.001, keep_alive=lambda: None, keep_alive_interval=5)
    error = error_of(
        inspect_archive, source, tmp_path / "work" / "a.json", tmp_path / "work", LIMITS, hurried
    )
    assert error.error_class == "zip_too_complex"


def test_many_small_entries_can_add_up_to_a_bomb(capsys):
    """Each entry stays under the ratio floor, but the archive as a whole doesn't."""
    budget = zip_child.Budget(10**9, max_ratio=100, ratio_min_bytes=16 * KIB)
    with pytest.raises(SystemExit):
        for _ in range(10):
            budget.start_entry(10)  # 10 compressed bytes ...
            budget.add(5 * KIB, 10, 5 * KIB)  # ... that became 5 KiB
    assert '"error_class": "zip_bomb"' in capsys.readouterr().out
