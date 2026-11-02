"""Fixtures for integration tests, which run against a real PostgreSQL *test* database.

Isolation from your development data:
- Tests use a separate database named "<POSTGRES_DB>_test" (e.g. "tasklane_test"). It is
  dropped, recreated, and migrated once per test run; your dev database is never touched.
- A guard refuses any database name that doesn't end in "_test" or equals POSTGRES_DB.
- Every table is emptied before each test that uses the `engine` or `session` fixture. (Rolling
  back a transaction per test isn't enough: the race tests need two real connections that see
  each other's commits.)
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import models  # noqa: F401  (importing registers every table on Base.metadata)
from app.db.base import Base
from app.db.session import create_db_engine
from tests.db_utils import recreate_database, run_alembic, settings_for_database

TABLES = [table.name for table in Base.metadata.sorted_tables]
assert TABLES, "no tables registered: is app.db.models imported?"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test in this folder as `integration` (so `pytest -m "not integration"` skips
    them and `pytest -m integration` selects them)."""
    for item in items:
        if "integration" in item.path.parts:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def dev_settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def test_settings(dev_settings: Settings) -> Settings:
    return settings_for_database(dev_settings, f"{dev_settings.postgres_db}_test")


@pytest.fixture(scope="session")
def migrated_engine(dev_settings: Settings, test_settings: Settings) -> Iterator[Engine]:
    recreate_database(test_settings, dev_settings.postgres_db)
    engine = create_db_engine(test_settings)
    run_alembic(engine, "upgrade", "head")  # build the schema with the real migrations
    yield engine
    engine.dispose()


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    with migrated_engine.begin() as connection:
        # If an earlier test leaked an open transaction, fail clearly instead of hanging forever
        # (TRUNCATE needs an exclusive lock on every table).
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    return migrated_engine


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as db_session:
        yield db_session
