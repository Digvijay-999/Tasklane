"""The heartbeat: a background thread that keeps a running attempt's lease alive.

Why a thread: stage code renews the lease only when it reports something (progress, a provider
call). A stage that is busy and silent for longer than the lease (a slow parse, a long provider
call, a bug that blocks) would look dead to recovery. The heartbeat renews the lease every
`interval` seconds (a quarter of the lease by default) no matter what the stage is doing.

Lifecycle inside a Celery prefork worker:
- Celery forks its worker processes *before* any task runs. The heartbeat is created inside a
  task, in the forked process, after the claim committed, and stopped in a `finally` block
  before the task returns. No thread ever exists across a fork, so the usual "fork with threads"
  hazards don't apply. (The sandboxed parsers are started with subprocess, which uses fork+exec
  in a way that is safe in a threaded process.)
- It is a daemon thread: it can never keep a worker process alive on its own. If the process is
  killed (kill -9, out of memory, the Celery hard time limit), the thread dies with it, the lease
  is no longer renewed, and recovery takes over once it expires.
- Each beat checks out its own pooled database connection, never the one the stage code is
  using, and commits on its own. A beat that fails (database briefly down) is logged and tried
  again at the next interval; one lease holds about three missed beats.
- If a renewal matches no open attempt, recovery (or someone else) has closed it: the worker is
  *fenced out*. The heartbeat sets `fenced` and stops. The execution service checks that flag
  before every write and at every keep-alive, and the parser sandbox checks it about once a
  second, killing its child process when it is set.

If the thread itself dies from an unexpected error, nothing breaks silently: it logs the error
and sets `died`; the lease is then renewed only by the stage's own writes, so a long, silent stage
may have its lease expire, and recovery retries the job. Losing the heartbeat can therefore cost
an unnecessary retry, never two workers owning the same job (that is what fencing prevents).

The flip side of an independent heartbeat: a stage that hangs forever would be kept alive
forever. Every stage therefore has its own time limit (parser deadlines, `stage_timeout`), and
the Celery hard time limit kills the whole process as a last resort.
"""

import logging
import threading
import uuid
from datetime import timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.db.repositories import attempts as attempt_rows

logger = logging.getLogger(__name__)


class Heartbeat:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        attempt_id: uuid.UUID,
        *,
        lease: timedelta,
        interval: float,
        log_extra: dict[str, object] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.attempt_id = attempt_id
        self.lease = lease
        self.interval = interval
        self.log_extra = log_extra or {}
        self.fenced = threading.Event()  # set once a renewal found the attempt closed
        self.died = False  # the thread ended because of an unexpected error
        self.beats = 0  # successful renewals (for tests and logs)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"heartbeat-{attempt_id}", daemon=True
        )

    def start(self) -> "Heartbeat":
        self._thread.start()
        return self

    def stop(self, timeout: float = 10.0) -> None:
        """Ask the thread to stop and wait for it. Safe to call more than once. A beat in
        progress finishes first (at most one short UPDATE)."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)
            if self._thread.is_alive():
                logger.warning("heartbeat thread did not stop in time", extra=self.log_extra)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def __enter__(self) -> "Heartbeat":
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.interval):
                if not self._beat():
                    # No log here: a beat racing the worker's own finishing transaction also sees
                    # the attempt closed. The execution service logs real fencing when it acts.
                    self.fenced.set()
                    return
        except BaseException:
            self.died = True
            logger.exception(
                "heartbeat thread died; the lease is no longer renewed in the background",
                extra=self.log_extra,
            )

    def _beat(self) -> bool:
        """One renewal in its own short transaction. True = still ours (or unknown: the database
        couldn't be reached, try again next time); False = fenced out."""
        try:
            with self.session_factory.begin() as session:
                renewed = attempt_rows.renew_lease(session, self.attempt_id, self.lease)
        except Exception:
            logger.warning(
                "heartbeat could not renew the lease; will try again",
                exc_info=True,
                extra=self.log_extra,
            )
            return True
        if renewed:
            self.beats += 1
        return renewed
