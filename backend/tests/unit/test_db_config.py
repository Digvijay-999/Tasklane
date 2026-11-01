import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db.session import build_database_url
from tests.conftest import TEST_PASSWORD


def test_url_escapes_special_characters_in_password(unit_settings):
    url = build_database_url(unit_settings)
    assert url.password == TEST_PASSWORD
    rendered = url.render_as_string(hide_password=False)
    assert "p%40ss%3Aword" in rendered  # '@' and ':' are percent-encoded


def test_url_hides_password_when_printed(unit_settings):
    assert TEST_PASSWORD not in str(build_database_url(unit_settings))


@pytest.mark.parametrize("value", [0, 1, 31])
def test_dependency_timeout_must_be_in_safe_range(unit_settings, value):
    # 0 means "wait forever" to libpq, and 1 is silently rounded up to 2.
    with pytest.raises(ValidationError):
        Settings(**{**unit_settings.model_dump(), "dependency_timeout_seconds": value})
