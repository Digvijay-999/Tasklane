"""Integration tests against the real PostgreSQL and Redis containers from compose.yaml.

Run inside Docker:  docker compose run --rm api pytest -m integration

The reconnection tests kill only the test app's *own* connections (by Postgres backend PID and
Redis client ID), so the running `api` container and future workers are not disturbed.
"""

from collections.abc import Iterator

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import get_settings
from app.db.session import create_db_engine
from app.main import create_app
from app.observability.logging import JsonFormatter

pytestmark = pytest.mark.integration


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(get_settings())) as test_client:
        yield test_client


def test_ready_with_real_dependencies(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "queue": "ok"}}


def test_ready_recovers_after_database_connections_are_killed(client):
    engine = client.app.state.db_engine
    # Find the PID of the connection the app has pooled (it goes back to the pool afterwards).
    with engine.connect() as connection:
        pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
    assert engine.pool.checkedin() == 1

    # Kill it on the server side, as a Postgres restart would.
    admin = create_db_engine(get_settings())
    try:
        with admin.connect() as connection:
            killed = connection.execute(
                text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
            ).scalar_one()
    finally:
        admin.dispose()
    assert killed is True

    # pool_pre_ping detects the dead connection and replaces it; no API restart needed.
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["database"] == "ok"


def test_ready_recovers_after_redis_connections_are_killed(client):
    broker = client.app.state.broker_client
    client_id = broker.client_id()  # the ID of the app's pooled Redis connection

    admin = redis.Redis.from_url(get_settings().redis_url)
    try:
        assert admin.client_kill_filter(_id=client_id) == 1
    finally:
        admin.close()

    assert client.get("/health/ready").json()["checks"]["queue"] == "ok"


def test_real_driver_errors_do_not_leak_into_response_or_logs(caplog):
    settings = get_settings().model_copy(
        update={
            "postgres_host": "postgres",
            "postgres_port": 1,  # nothing listens here: psycopg raises a real OperationalError
            "redis_url": "redis://redis:1/0",
        }
    )
    password = settings.postgres_password.get_secret_value()

    with TestClient(create_app(settings)) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "unavailable", "queue": "unavailable"}
    log_lines = [JsonFormatter().format(record) for record in caplog.records]
    for sensitive in (password, "postgres:1", "redis:1"):
        assert sensitive not in response.text
        assert not any(sensitive in line for line in log_lines)
