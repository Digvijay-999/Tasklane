"""Helpers for creating and migrating throwaway test databases.

Safety: every function that creates or drops a database calls `assert_safe_test_database` first,
so tests can never drop or modify the development database.
"""

import re
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.db.session import build_database_url

BACKEND_DIR = Path(__file__).resolve().parents[1]
_TEST_DB_NAME = re.compile(r"^[a-z0-9_]+_test$")


class UnsafeTestDatabaseError(RuntimeError):
    pass


def assert_safe_test_database(name: str, dev_database: str) -> None:
    """Refuse any database that isn't clearly a test database."""
    if name == dev_database:
        raise UnsafeTestDatabaseError(f"refusing to use the development database {name!r}")
    if not _TEST_DB_NAME.fullmatch(name):
        raise UnsafeTestDatabaseError(
            f"test database names must be lowercase and end with '_test' (got {name!r})"
        )


def settings_for_database(base: Settings, name: str) -> Settings:
    assert_safe_test_database(name, base.postgres_db)
    return base.model_copy(update={"postgres_db": name})


def recreate_database(settings: Settings, dev_database: str) -> None:
    """Drop (if present) and create an empty database named settings.postgres_db."""
    name = settings.postgres_db
    assert_safe_test_database(name, dev_database)
    # CREATE/DROP DATABASE can't run inside a transaction or while connected to that database,
    # so connect to the built-in "postgres" maintenance database in autocommit mode.
    admin = create_engine(
        build_database_url(settings.model_copy(update={"postgres_db": "postgres"})),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    try:
        with admin.connect() as connection:
            # `name` is validated above (lowercase letters, digits, underscores), so quoting it
            # directly is safe. Database names can't be passed as bind parameters.
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()


def alembic_config() -> Config:
    return Config(str(BACKEND_DIR / "alembic.ini"))


def run_alembic(engine: Engine, action: str, revision: str) -> None:
    """Run `alembic upgrade|downgrade <revision>` against `engine`'s database in one transaction."""
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        getattr(command, action)(config, revision)


def wait_until_blocked_by(engine: Engine, holder_pid: int, timeout: float = 5.0) -> None:
    """Block until some session is waiting on a lock held by the session with `holder_pid`.

    Race tests use this instead of sleeping: it proves the second transaction really is blocked
    behind the first (and not on something unrelated) before the first commits.
    """
    deadline = time.monotonic() + timeout
    with engine.connect() as connection:
        while time.monotonic() < deadline:
            waiting = connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE :holder = ANY(pg_blocking_pids(pid))"
                ),
                {"holder": holder_pid},
            ).scalar_one()
            if waiting:
                return
            connection.rollback()  # end the snapshot so the next poll sees fresh activity
            time.sleep(0.02)
    raise TimeoutError(f"no session became blocked by backend {holder_pid}")


def backend_pid(session_or_connection) -> int:  # type: ignore[no-untyped-def]
    """The Postgres server process ID serving this session (identifies its locks)."""
    return session_or_connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
