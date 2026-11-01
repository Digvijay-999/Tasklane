import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.api.health import get_check_timeout, get_database_check, run_checks
from app.observability.logging import JsonFormatter
from tests.conftest import TEST_PASSWORD


def test_live_returns_ok(make_client):
    response = make_client().get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_live_does_not_depend_on_database(make_client):
    response = make_client(database_ok=False, broker_ok=False).get("/health/live")
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("database_ok", "broker_ok", "expected_code", "expected_body"),
    [
        (True, True, 200, {"status": "ready", "checks": {"database": "ok", "queue": "ok"}}),
        # Redis down: the API can still serve durable job state, so it stays ready.
        (
            True,
            False,
            200,
            {"status": "ready", "checks": {"database": "ok", "queue": "unavailable"}},
        ),
        (
            False,
            True,
            503,
            {"status": "not_ready", "checks": {"database": "unavailable", "queue": "ok"}},
        ),
        (
            False,
            False,
            503,
            {"status": "not_ready", "checks": {"database": "unavailable", "queue": "unavailable"}},
        ),
    ],
)
def test_ready_reports_each_dependency(
    make_client, database_ok, broker_ok, expected_code, expected_body
):
    response = make_client(database_ok=database_ok, broker_ok=broker_ok).get("/health/ready")
    assert response.status_code == expected_code
    assert response.json() == expected_body


def test_ready_does_not_wait_longer_than_timeout_for_a_hung_dependency(make_client, caplog):
    release = threading.Event()
    client = make_client()
    client.app.dependency_overrides[get_database_check] = lambda: lambda: release.wait(5)
    client.app.dependency_overrides[get_check_timeout] = lambda: 0.2
    try:
        started = time.perf_counter()
        response = client.get("/health/ready")
        elapsed = time.perf_counter() - started
    finally:
        release.set()  # let the background check thread finish

    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "unavailable", "queue": "ok"}
    assert elapsed < 1.0
    assert any(getattr(r, "error_type", None) == "Timeout" for r in caplog.records)


def test_queued_checks_are_cancelled_instead_of_piling_up():
    release = threading.Event()
    ran: list[str] = []
    executor = ThreadPoolExecutor(max_workers=1)  # one thread, so the second check must queue
    try:
        results = run_checks(
            {
                "database": lambda: release.wait(5),
                "queue": lambda: ran.append("queue"),
            },
            timeout=0.2,
            executor=executor,
        )
    finally:
        release.set()
        executor.shutdown(wait=True)

    assert results == {"database": "unavailable", "queue": "unavailable"}
    assert ran == []  # the queued check was cancelled, never run late


def test_ready_failure_does_not_leak_error_details(make_client, caplog):
    response = make_client(database_ok=False, broker_ok=False).get("/health/ready")
    assert TEST_PASSWORD not in response.text

    # Render every captured record exactly as it would appear in the container logs.
    log_lines = [JsonFormatter().format(record) for record in caplog.records]
    assert not any(TEST_PASSWORD in line for line in log_lines)

    failures = [r for r in caplog.records if r.getMessage() == "dependency check failed"]
    assert {(r.dependency, r.error_type) for r in failures} == {
        ("database", "ConnectionError"),
        ("queue", "ConnectionError"),
    }
