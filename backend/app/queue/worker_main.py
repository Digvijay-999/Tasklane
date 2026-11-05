"""The Celery app used by the `celery worker` command:

    celery -A app.queue.worker_main worker --loglevel=INFO --hostname=worker@%h

Kept separate from celery_app.py so that importing the factory (e.g. in tests or the dispatcher)
never creates an app from environment settings as a side effect.
"""

from celery.signals import setup_logging, worker_process_shutdown

from app.config import get_settings
from app.observability.logging import configure_logging
from app.queue.celery_app import create_celery_app
from app.queue.worker_process import shutdown_worker_process

settings = get_settings()
celery_app = create_celery_app(settings)


@setup_logging.connect
def _configure_logging(**_: object) -> None:
    # Connecting to this signal stops Celery from installing its own logging; we use JSON logs.
    configure_logging(settings.log_level)


@worker_process_shutdown.connect
def _close_database_connections(**_: object) -> None:
    shutdown_worker_process()
