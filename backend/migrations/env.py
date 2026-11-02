"""Alembic environment: how migrations connect to the database.

- Normal use (`alembic upgrade head`): connect using the app's settings from environment variables.
- Tests: pass an open connection via `config.attributes["connection"]`, so tests can migrate a
  separate test database without touching development data.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.db import models  # noqa: F401  (importing registers every table on Base.metadata)
from app.db.base import Base

config = context.config
target_metadata = Base.metadata

# Configure plain-text logging for the CLI, but not when a test supplies the connection
# (that would replace pytest's own log capture).
if config.config_file_name is not None and "connection" not in config.attributes:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`: print the SQL instead of running it."""
    context.configure(
        url="postgresql+psycopg://",  # only the dialect matters when generating SQL
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return

    from app.config import get_settings  # imported here so --sql works without env vars
    from app.db.session import build_database_url

    engine = create_engine(build_database_url(get_settings()), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            _run(connection)
    finally:
        engine.dispose()


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    # compare_server_default: also report changed column defaults in `alembic check` and
    # autogenerate (off by default in Alembic).
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_server_default=True
    )
    # Postgres can roll back schema changes, so a migration that fails halfway is undone entirely.
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
