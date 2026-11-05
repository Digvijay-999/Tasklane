"""LocalStorage: generated paths, temporary writes, atomic commit."""

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
