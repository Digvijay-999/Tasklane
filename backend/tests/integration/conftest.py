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
import redis
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import models  # noqa: F401  (importing registers every table on Base.metadata)
from app.db.base import Base
from app.db.session import create_db_engine
from app.main import create_app
from app.storage.local import LocalStorage
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


# --- API fixtures (Phase 3) --------------------------------------------------------------------

# Small limits so size tests don't need megabytes of data.
TEST_MAX_PDF_BYTES = 4096
TEST_MAX_ZIP_BYTES = 8192


@pytest.fixture
def api_settings(test_settings: Settings, tmp_path) -> Settings:
    """Test-database settings with a fresh storage folder per test and small upload limits."""
    return test_settings.model_copy(
        update={
            "storage_root": tmp_path / "storage",
            "max_pdf_upload_bytes": TEST_MAX_PDF_BYTES,
            "max_zip_upload_bytes": TEST_MAX_ZIP_BYTES,
        }
    )


@pytest.fixture
def api(engine: Engine, api_settings: Settings) -> Iterator[TestClient]:
    """A TestClient for the full app, on an emptied test database."""
    with TestClient(create_app(api_settings)) as client:
        yield client


@pytest.fixture
def storage(api_settings: Settings) -> LocalStorage:
    return LocalStorage(api_settings.storage_root)


# --- Queue fixtures (Phase 4) ------------------------------------------------------------------

TEST_REDIS_DB = 15
TEST_QUEUE = "tasklane-test"


def _test_redis_url(dev_redis_url: str) -> str:
    """The dev Redis URL pointed at database 15 instead of the dev database."""
    base, _, last = dev_redis_url.rstrip("/").rpartition("/")
    if not last.isdigit():  # URL had no database number
        base = dev_redis_url.rstrip("/")
    url = f"{base}/{TEST_REDIS_DB}"
    assert url != dev_redis_url, "refusing to use the development Redis database for tests"
    return url


@pytest.fixture
def queue_settings(api_settings: Settings, dev_settings: Settings) -> Settings:
    """Test settings for anything that touches the broker: Redis database 15, test queue, and
    fast timings. A running dev worker (database 0, queue "tasklane") never sees these."""
    return api_settings.model_copy(
        update={
            "redis_url": _test_redis_url(dev_settings.redis_url),
            "celery_queue": TEST_QUEUE,
            "dispatch_poll_seconds": 0.2,
            "dummy_stage_seconds": 0.0,
            "lease_seconds": 30,
            "provider_timeout_seconds": 10,  # must stay <= lease / 2 (validated in Settings)
        }
    )


@pytest.fixture
def broker(queue_settings: Settings) -> Iterator[redis.Redis]:
    """A client for the empty test Redis database (emptied before and after each test)."""
    client = redis.Redis.from_url(queue_settings.redis_url)
    assert client.connection_pool.connection_kwargs.get("db") == TEST_REDIS_DB
    client.flushdb()
    yield client
    client.flushdb()
    client.close()
