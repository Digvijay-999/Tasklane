"""Shared test fixtures (pytest discovers this file automatically)."""

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.health import get_broker_check, get_database_check
from app.config import Settings
from app.main import create_app

TEST_PASSWORD = "unit-test-secret-p@ss:word"


@pytest.fixture
def unit_settings() -> Settings:
    """Settings for unit tests. Nothing connects to these hosts; checks are replaced by fakes."""
    return Settings(
        postgres_user="tasklane",
        postgres_password=TEST_PASSWORD,
        postgres_db="tasklane",
        postgres_host="db.invalid",
        redis_url="redis://redis.invalid:6379/0",
    )


def _passing() -> None:
    return None


def _failing() -> None:
    raise ConnectionError(f"could not connect using password {TEST_PASSWORD}")


@pytest.fixture
def make_client(unit_settings: Settings) -> Iterator[Callable[..., TestClient]]:
    """Build a TestClient whose dependency checks pass or fail as requested."""
    clients: list[TestClient] = []

    def _make(database_ok: bool = True, broker_ok: bool = True) -> TestClient:
        app = create_app(unit_settings)
        app.dependency_overrides[get_database_check] = lambda: _passing if database_ok else _failing
        app.dependency_overrides[get_broker_check] = lambda: _passing if broker_ok else _failing
        client = TestClient(app)
        client.__enter__()  # runs the app's startup (lifespan)
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)
