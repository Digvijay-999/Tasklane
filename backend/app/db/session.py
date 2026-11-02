"""Database engine setup and connectivity check.

The SQLAlchemy *engine* owns a *connection pool*: a small set of open PostgreSQL connections that
are reused across requests (similar to the pool inside a Node `pg.Pool`).
"""

from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings

APPLICATION_NAME = "tasklane-api"


def build_database_url(settings: Settings) -> URL:
    """Build the connection URL from parts, so passwords with ':' or '@' are escaped safely."""
    return URL.create(
        drivername="postgresql+psycopg",
        username=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
    )


def create_db_engine(settings: Settings) -> Engine:
    """Create the engine. No connection is opened until the first query."""
    return create_engine(
        build_database_url(settings),
        # Before handing a pooled connection to our code, run a tiny "ping" on it. If the
        # connection is dead (e.g. Postgres was restarted), SQLAlchemy throws it away, invalidates
        # the other pooled connections from before the restart, and opens a fresh one. This is
        # what lets the API recover after Postgres restarts without restarting the API itself.
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=10,  # max seconds to wait for a free pooled connection
        connect_args={
            # Fail fast instead of hanging when Postgres is unreachable (libpq: whole seconds).
            "connect_timeout": settings.dependency_timeout_seconds,
            # Shows up in pg_stat_activity, which makes our connections easy to identify.
            "application_name": APPLICATION_NAME,
            # connect_timeout only covers *opening* a connection. If an open connection silently
            # stops responding (network drop, no reset), these TCP settings make the OS give up
            # within ~30-60 s instead of the default of many minutes.
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
            "tcp_user_timeout": 30_000,  # milliseconds
        },
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a factory for ORM sessions (a session is one unit of work / one transaction).

    expire_on_commit=False keeps loaded objects readable after commit, so code doesn't trigger
    surprise extra queries when it reads an attribute after committing.
    """
    return sessionmaker(bind=engine, expire_on_commit=False)


def check_database(engine: Engine) -> None:
    """Raise if PostgreSQL cannot answer a trivial query."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
