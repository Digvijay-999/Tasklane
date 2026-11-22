"""Worker presence: each worker process says "I'm alive" in the database every few seconds.

The API answers "is any worker running?" from the worker_heartbeats table (GET /v1/overview, the
tasklane_workers_alive metric) instead of asking Celery over Redis: the database is the source
of truth, the check works while Redis is down, and it needs no broker round trip.

Each Celery *child* process (the ones that run jobs) starts a WorkerPresence thread when it is
forked (worker_process_init) and stops it on a clean exit (marking the row stopped, so the
dashboard updates at once). A process that is killed just stops reporting; it counts as gone
once it has missed three intervals.

The row also carries `WorkerCounters`: things only the worker process itself can observe and
that leave no row in the database (an attempt it gave up because it was fenced out, a heartbeat
beat that failed, a heartbeat thread that died). They are kept in memory and saved with every
report, so they outlive the process (at most one interval of increments is lost on kill -9).
"""

import logging
import secrets
import threading

from sqlalchemy.orm import Session, sessionmaker

from app.db.repositories import workers as worker_rows

logger = logging.getLogger(__name__)


class WorkerCounters:
    """Thread-safe in-memory counters of one worker process (see the module docstring)."""

    NAMES = worker_rows.COUNTER_COLUMNS

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values = dict.fromkeys(self.NAMES, 0)

    def add(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._values[name] += amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)


# One per process. A forked child starts with the parent's values, which are always zero: the
# Celery parent process never runs jobs.
COUNTERS = WorkerCounters()


class WorkerPresence:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        worker_id: str,
        *,
        interval: float,
        counters: WorkerCounters = COUNTERS,
    ) -> None:
        self.session_factory = session_factory
        self.worker_id = worker_id
        # The row key is unique per process instance: after a container restart, a new process
        # often gets the same host name and pid, and must not overwrite (reset) the counters the
        # previous one saved.
        self.row_id = f"{worker_id}#{secrets.token_hex(4)}"
        self.interval = interval
        self.counters = counters
        self._stop = threading.Event()
        self._failing = False
        self._thread = threading.Thread(target=self._run, name="worker-presence", daemon=True)

    def start(self) -> "WorkerPresence":
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Stop reporting and mark the row stopped (best effort: if the database is down, the
        row simply goes stale)."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)
        try:
            with self.session_factory.begin() as session:
                worker_rows.mark_stopped(session, self.row_id, self.counters.snapshot())
        except Exception as exc:
            logger.warning(
                "could not mark the worker stopped",
                extra={"worker_id": self.worker_id, "error_type": type(exc).__name__},
            )

    def report(self) -> bool:
        """One report in its own short transaction. Logs once per outage, not every interval."""
        try:
            with self.session_factory.begin() as session:
                worker_rows.record_presence(session, self.row_id, self.counters.snapshot())
        except Exception as exc:
            if not self._failing:
                logger.warning(
                    "worker presence could not be recorded; will keep trying",
                    extra={"worker_id": self.worker_id, "error_type": type(exc).__name__},
                )
            self._failing = True
            return False
        if self._failing:
            logger.info("worker presence recorded again", extra={"worker_id": self.worker_id})
        self._failing = False
        return True

    def _run(self) -> None:
        self.report()
        while not self._stop.wait(self.interval):
            self.report()
