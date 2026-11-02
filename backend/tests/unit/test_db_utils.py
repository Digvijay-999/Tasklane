"""The guard that keeps tests away from the development database."""

import pytest

from tests.db_utils import UnsafeTestDatabaseError, assert_safe_test_database, settings_for_database


@pytest.mark.parametrize("name", ["tasklane_test", "tasklane_migrations_test", "x_test"])
def test_accepts_test_database_names(name):
    assert_safe_test_database(name, dev_database="tasklane")


@pytest.mark.parametrize(
    "name",
    [
        "tasklane",  # the dev database itself
        "tasklane_testing",  # doesn't end with _test
        "test_tasklane",
        "Tasklane_test",  # uppercase not allowed
        'tasklane_test"; DROP DATABASE tasklane; --',  # can't smuggle SQL into the name
        "",
    ],
)
def test_rejects_anything_else(name):
    with pytest.raises(UnsafeTestDatabaseError):
        assert_safe_test_database(name, dev_database="tasklane")


def test_rejects_dev_database_even_if_it_looks_like_a_test_name():
    with pytest.raises(UnsafeTestDatabaseError):
        assert_safe_test_database("app_test", dev_database="app_test")


def test_settings_for_database_only_changes_the_database_name(unit_settings):
    settings = settings_for_database(unit_settings, "tasklane_test")
    assert settings.postgres_db == "tasklane_test"
    assert settings.postgres_host == unit_settings.postgres_host
    assert unit_settings.postgres_db == "tasklane"  # original untouched
