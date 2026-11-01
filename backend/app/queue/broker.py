"""Redis broker client and connectivity check.

Celery is added in Phase 4. For now the API only needs to report whether the broker is reachable.
"""

import redis

from app.config import Settings


def create_broker_client(settings: Settings) -> redis.Redis:
    """Create a Redis client. Like the DB engine, it connects lazily and pools connections.

    If a pooled connection breaks (e.g. Redis restarted), redis-py drops it and opens a new one
    on the next command, so no extra configuration is needed for reconnection.
    """
    timeout = settings.dependency_timeout_seconds
    return redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
        health_check_interval=30,  # re-check idle connections before reuse
    )


def check_broker(client: redis.Redis) -> None:
    """Raise if Redis does not answer PING."""
    client.ping()
