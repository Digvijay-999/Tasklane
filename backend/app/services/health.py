"""Dependency checks for /health/ready, built from the app's database engine and Redis client.

They live here so the api layer never imports the db or queue packages directly.
"""

from collections.abc import Callable

import redis
from sqlalchemy import Engine

from app.db.session import check_database
from app.queue.broker import check_broker

DependencyCheck = Callable[[], None]


def database_check(engine: Engine) -> DependencyCheck:
    """A check that raises if PostgreSQL can't answer `SELECT 1`."""
    return lambda: check_database(engine)


def broker_check(client: redis.Redis) -> DependencyCheck:
    """A check that raises if Redis doesn't answer PING."""
    return lambda: check_broker(client)
