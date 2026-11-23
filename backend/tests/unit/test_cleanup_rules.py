"""Pure parts of the cleanup (app/maintenance/cleanup.py) and its settings."""

import uuid

import pytest
from pydantic import ValidationError

from app.maintenance.cleanup import _as_uuid, _is_temp
from tests.unit.test_provider_settings import settings


def test_only_canonical_uuid_folder_names_count_as_ours():
    value = uuid.uuid4()
    assert _as_uuid(str(value)) == value
    for name in (str(value).upper(), value.hex, f"{value}.bak", "..", "notes", ""):
        assert _as_uuid(name) is None


def test_temporary_files_are_the_atomic_writers_names():
    assert _is_temp("work/j/.chunks.jsonl.0a1b2c.tmp")
    assert _is_temp("work/j/summaries/.c1.json.ff.tmp")
    assert not _is_temp("work/j/chunks.jsonl")
    assert not _is_temp("work/j/notes.tmp")  # not hidden: not one of ours
    assert not _is_temp("work/.j/chunks.jsonl")


def test_published_outbox_retention_must_cover_the_lost_message_grace():
    with pytest.raises(ValidationError, match="RETENTION_PUBLISHED_OUTBOX_HOURS"):
        settings(retention_published_outbox_hours=0.01, lost_message_grace_seconds=300)
    assert settings(retention_published_outbox_hours=1).cleanup_enabled is False
