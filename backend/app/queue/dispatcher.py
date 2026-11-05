"""The outbox dispatcher: publishes committed outbox rows to the queue.

Run it with:  python -m app.queue.dispatcher

Why an outbox: the API writes the job and its outbox row in one database transaction, so a job
can never exist without its "please run me" message, even if Redis is down at that moment. This
process then moves those rows into Redis.

One pass (Dispatcher.run_once) is ONE database transaction:
1. Lock up to `batch_size` due rows: `published_at IS NULL AND available_at <= now()`, oldest
   first, `FOR UPDATE SKIP LOCKED`. Other dispatchers skip locked rows instead of waiting, so two
   dispatchers never publish the same row at the same time.
2. Publish each row to Celery while holding the lock.
3. Mark the published rows (`published_at = now()`), then commit.

Crash windows:
- Crash after locking, before publishing: the transaction rolls back, the locks are released,
  nothing was sent. The next pass publishes the rows. Nothing is lost.
- Crash after publishing, before the commit: the message is in Redis, but the row still looks
  unpublished. The next pass publishes it AGAIN -> a duplicate message. That is acceptable by
  design: the worker's claim drops duplicates.
- Redis accepts a message and then loses it (crash before saving): the row says published but no
  message exists, so the job stays `queued`. Redis's append-only file (compose.yaml) makes this
  unlikely; Phase 7 recovery re-queues jobs stuck like this.

If Redis is unavailable, publishing fails: the failed row and the rest of the batch get
`dispatch_attempts + 1`, the error type, and a delayed `available_at` (1 s, 2 s, 4 s, ... up to
60 s), and the loop itself backs off. The dispatcher never gives up; the jobs stay `queued` and
visible through the API, and go out as soon as Redis is back.

This module contains only dispatching. The recovery loop (Phase 7) is a separate module that will
run in the same process.
"""

import logging
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from celery import Celery
from sqlalchemy.orm import Session, sessionmaker

from app.db.repositories import outbox as outbox_rows
from app.db.repositories.outbox import DueMessage
from app.domain.dispatch import dispatch_backoff_seconds
from app.queue.celery_app import CELERY_TASK_NAMES

# A fixed name: when run with `python -m`, __name__ would be "__main__".
logger = logging.getLogger("tasklane.dispatcher")


class Publisher(Protocol):
    def publish(self, message: DueMessage) -> None:
        """Send the message to the queue. Raise if the broker can't be reached."""
        ...


class CeleryPublisher:
    def __init__(self, celery_app: Celery, queue: str) -> None:
        self.celery_app = celery_app
        self.queue = queue

    def publish(self, message: DueMessage) -> None:
        self.celery_app.send_task(
            CELERY_TASK_NAMES[message.task_name],
            kwargs=dict(message.payload),
            queue=self.queue,
            # The dispatcher owns retries and backoff; don't let Celery block retrying.
            retry=False,
            headers={"outbox_id": message.id},
        )


@dataclass(frozen=True)
class PassResult:
    published: int
    failed: int
    broker_error: str | None = None  # error type when publishing failed


class Dispatcher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        publisher: Publisher,
        *,
        batch_size: int,
        backoff: Callable[[int], float] = dispatch_backoff_seconds,
    ) -> None:
        self.session_factory = session_factory
        self.publisher = publisher
        self.batch_size = batch_size
        self.backoff = backoff

    def run_once(self) -> PassResult:
        """One pass in one transaction (see the module docstring)."""
        with self.session_factory.begin() as session:
            due = outbox_rows.claim_due_messages(session, self.batch_size)
            published: list[int] = []
            for index, message in enumerate(due):
                try:
                    self.publisher.publish(message)
                except Exception as exc:
                    error_type = type(exc).__name__
                    # The broker is most likely down: the rest of the batch would fail the same
                    # way, so record the failure on all of them and stop.
                    for failed in due[index:]:
                        outbox_rows.mark_dispatch_failed(
                            session,
                            failed.id,
                            error_type=error_type,
                            retry_in=timedelta(seconds=self.backoff(failed.dispatch_attempts + 1)),
                        )
                    outbox_rows.mark_published(session, published)
                    logger.warning(
                        "publishing failed; will retry",
                        extra={
                            "error_type": error_type,
                            "published": len(published),
                            "failed": len(due) - index,
                        },
                    )
                    return PassResult(len(published), len(due) - index, error_type)
                published.append(message.id)
                logger.info(
                    "outbox message published",
                    extra={"outbox_id": message.id, "job_id": str(message.job_id)},
                )
            outbox_rows.mark_published(session, published)
        return PassResult(len(published), 0)


def run_forever(
    dispatcher: Dispatcher,
    *,
    poll_seconds: float,
    stop: threading.Event,
    backoff: Callable[[int], float] = dispatch_backoff_seconds,
) -> None:
    """Poll until `stop` is set. Back off while Redis or Postgres is unavailable."""
    consecutive_failures = 0
    while not stop.is_set():
        try:
            result = dispatcher.run_once()
        except Exception:
            # Usually the database being unreachable. Nothing was published or marked.
            consecutive_failures += 1
            logger.exception("dispatch pass failed; backing off")
            stop.wait(backoff(consecutive_failures))
            continue

        if result.broker_error:
            consecutive_failures += 1
            stop.wait(backoff(consecutive_failures))
            continue
        consecutive_failures = 0
        if result.published >= dispatcher.batch_size:
            continue  # a full batch: more rows are probably waiting, go again immediately
        stop.wait(poll_seconds)


def main() -> None:
    from app.config import get_settings
    from app.db.session import create_db_engine, create_session_factory
    from app.observability.logging import configure_logging
    from app.queue.celery_app import create_celery_app

    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(
        settings, pool_size=2, max_overflow=0, application_name="tasklane-dispatcher"
    )
    dispatcher = Dispatcher(
        create_session_factory(engine),
        CeleryPublisher(create_celery_app(settings, for_publishing=True), settings.celery_queue),
        batch_size=settings.dispatch_batch_size,
    )

    stop = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        logger.info("dispatcher stopping", extra={"signal": signal.Signals(signum).name})
        stop.set()  # the current pass finishes; the loop exits before the next one

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    logger.info(
        "dispatcher started",
        extra={
            "poll_seconds": settings.dispatch_poll_seconds,
            "batch": settings.dispatch_batch_size,
        },
    )
    try:
        run_forever(dispatcher, poll_seconds=settings.dispatch_poll_seconds, stop=stop)
    finally:
        engine.dispose()
        logger.info("dispatcher stopped")


if __name__ == "__main__":
    main()
