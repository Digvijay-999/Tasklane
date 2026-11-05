"""Per-process state for Celery worker processes (database engine and execution service).

Celery's default worker starts a parent process and *forks* child processes that run the tasks.
A forked child is a copy of its parent, including open sockets. If the parent had opened database
connections, every child would share the same sockets and their queries would get mixed up (like
Node `cluster` workers sharing one `pg` connection).

So: nothing here is created at import time or in the parent. The first task a process runs
creates that process's own engine. The process id is recorded, and if this module ever finds
itself in a different process than the one that created the state (a fork happened after
creation), it discards the inherited state without closing the parent's connections and builds
its own.
"""

import logging
import os
import socket
import threading
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Engine

from app.config import get_settings
from app.db.session import create_db_engine, create_session_factory
from app.services.execution import JobExecutionService
from app.workflows.dummy import stages_for

logger = logging.getLogger(__name__)

# Each worker process runs one task at a time (prefetch 1), so it needs very few connections.
WORKER_POOL_SIZE = 2


@dataclass
class _ProcessState:
    pid: int
    engine: Engine
    service: JobExecutionService


_state: _ProcessState | None = None
_lock = threading.Lock()


def worker_id() -> str:
    """worker@<container hostname>:<pid>. The pid matters: a container runs several processes."""
    return f"worker@{socket.gethostname()}:{os.getpid()}"


def get_execution_service() -> JobExecutionService:
    global _state
    with _lock:
        if _state is not None and _state.pid != os.getpid():
            # Inherited from a parent process: never use (or close) the parent's connections.
            _state.engine.dispose(close=False)
            _state = None
        if _state is None:
            _state = _create_state()
        return _state.service


def shutdown_worker_process() -> None:
    global _state
    with _lock:
        if _state is not None and _state.pid == os.getpid():
            _state.engine.dispose()
        _state = None


def _create_state() -> _ProcessState:
    settings = get_settings()
    engine = create_db_engine(
        settings, pool_size=WORKER_POOL_SIZE, max_overflow=0, application_name="tasklane-worker"
    )
    service = JobExecutionService(
        create_session_factory(engine),
        worker_id=worker_id(),
        lease=timedelta(seconds=settings.lease_seconds),
        stages_for=lambda workflow_type: stages_for(
            workflow_type, dummy_stage_seconds=settings.dummy_stage_seconds
        ),
    )
    logger.info("worker process ready", extra={"worker_id": service.worker_id})
    return _ProcessState(pid=os.getpid(), engine=engine, service=service)
