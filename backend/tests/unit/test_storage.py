"""LocalStorage: generated paths, temporary writes, atomic commit."""

import os
import uuid

import pytest

from app.domain.enums import UploadKind
from app.storage.local import LocalStorage, StoragePathError


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "storage")


def test_paths_are_generated_from_ids_only(storage):
    upload_id = uuid.uuid4()
    assert storage.upload_path(upload_id, UploadKind.PDF) == f"uploads/{upload_id}/source.pdf"
    assert storage.incoming_path(upload_id) == f"uploads/.incoming/{upload_id}.part"


@pytest.mark.parametrize("relative", ["../outside.txt", "uploads/../../outside", "/etc/passwd"])
def test_paths_cannot_escape_the_root(storage, relative):
    with pytest.raises(StoragePathError):
        storage.absolute(relative)


def test_write_then_commit_moves_the_whole_file(storage):
    upload_id = uuid.uuid4()
    final = storage.upload_path(upload_id, UploadKind.ZIP)

    handle = storage.open_incoming(upload_id)
    handle.write(b"PK\x03\x04data")
    LocalStorage.finish_writing(handle)
    assert not storage.exists(final)

    storage.commit_incoming(upload_id, final)
    assert storage.absolute(final).read_bytes() == b"PK\x03\x04data"
    assert not storage.exists(storage.incoming_path(upload_id))


def test_open_incoming_refuses_to_overwrite(storage):
    upload_id = uuid.uuid4()
    storage.open_incoming(upload_id).close()
    with pytest.raises(FileExistsError):
        storage.open_incoming(upload_id)


def test_delete_is_idempotent(storage):
    upload_id = uuid.uuid4()
    storage.open_incoming(upload_id).close()
    storage.delete(storage.incoming_path(upload_id))
    storage.delete(storage.incoming_path(upload_id))  # already gone: no error
    assert not storage.exists(storage.incoming_path(upload_id))


# --- Cleanup helpers (Phase 12) -------------------------------------------------------------------


def test_cleanup_refuses_the_root_escapes_and_symlinks(storage, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (storage.root / "work").mkdir(parents=True)
    os.symlink(outside, storage.root / "work" / "link")

    for relative in (".", "work/..", "../outside", "work/link"):
        with pytest.raises(StoragePathError):
            storage.remove_tree(relative)
    with pytest.raises(StoragePathError):
        storage.remove_file("work/link/keep.txt")  # through a symlinked folder
    assert (outside / "keep.txt").read_text() == "keep"


def test_cleanup_removes_a_symlink_itself_never_its_target(storage, tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("keep")
    (storage.root / "artifacts").mkdir(parents=True)
    os.symlink(target, storage.root / "artifacts" / "link.md")

    assert storage.files_under("artifacts") == ["artifacts/link.md"]
    assert storage.remove_file("artifacts/link.md") is not None
    assert target.read_text() == "keep"


def test_removing_reports_bytes_freed_or_none_when_already_gone(storage):
    folder = storage.root / "work" / "job"
    (folder / "summaries").mkdir(parents=True)
    (folder / "a.json").write_bytes(b"12345")
    (folder / "summaries" / "b.json").write_bytes(b"123")

    assert storage.size_of("work/job") == 8
    assert storage.remove_file("work/job/a.json") == 5
    assert storage.remove_file("work/job/a.json") is None
    assert storage.remove_tree("work/job") == 3
    assert storage.remove_tree("work/job") is None
    assert storage.list_dir("work") == []
    assert storage.list_dir("missing") == []
