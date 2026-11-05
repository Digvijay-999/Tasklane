"""Worker processes never share database connections across a fork.

Celery's prefork pool forks child processes from a parent. This test forks a child from a
process that already has an execution service (and an open pooled connection) and checks that the
child builds its own engine (a different Postgres backend) and leaves the parent's connections
alone.
"""

import multiprocessing
import os

import pytest
from sqlalchemy import text

from app.queue import worker_process


def _backend_pid() -> int:
    service = worker_process.get_execution_service()
    with service.session_factory() as session:
        return session.execute(text("SELECT pg_backend_pid()")).scalar_one()


def _child(results: "multiprocessing.Queue[tuple[int, int, str]]") -> None:
    pid = _backend_pid()
    service = worker_process.get_execution_service()
    results.put((os.getpid(), pid, service.worker_id))


@pytest.fixture
def test_database_worker_settings(monkeypatch, queue_settings, engine):
    """Point the worker module at the test database (never the dev one)."""
    monkeypatch.setattr(worker_process, "get_settings", lambda: queue_settings)
    worker_process.shutdown_worker_process()
    yield
    worker_process.shutdown_worker_process()


def test_forked_child_gets_its_own_connection_and_leaves_the_parents_alone(
    test_database_worker_settings,
):
    parent_backend = _backend_pid()  # the parent now holds a pooled connection

    context = multiprocessing.get_context("fork")
    results = context.Queue()
    child = context.Process(target=_child, args=(results,))
    child.start()
    child_pid, child_backend, child_worker_id = results.get(timeout=20)
    child.join(timeout=20)

    assert child.exitcode == 0
    assert child_pid != os.getpid()
    assert child_backend != parent_backend  # a separate Postgres connection
    assert child_worker_id.endswith(f":{child_pid}")  # identity includes the child's pid

    # The child discarded the inherited pool without closing the parent's sockets, so the
    # parent's pooled connection still works.
    assert _backend_pid() == parent_backend


def test_worker_identity_format():
    assert worker_process.worker_id() == f"worker@{os.uname().nodename}:{os.getpid()}"
