"""FastAPI application factory.

Run with:  uvicorn app.main:create_app --factory
`--factory` tells uvicorn to call create_app() itself, so importing this module has no side
effects (handy for tests, which build their own app with test settings).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI

from app.api import events, health, jobs, uploads
from app.api.errors import install_error_handling
from app.config import Settings, get_settings
from app.db.session import create_db_engine, create_session_factory
from app.observability.logging import RequestContextMiddleware, configure_logging
from app.queue.broker import create_broker_client
from app.services.artifacts import ArtifactService
from app.services.events import EventService
from app.services.jobs import JobService
from app.services.uploads import UploadLimits, UploadService
from app.storage.local import LocalStorage

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    # "lifespan" runs once at startup (before `yield`) and once at shutdown (after it),
    # like setting things up before app.listen() in Express and closing them on SIGTERM.
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db_engine = create_db_engine(settings)
        app.state.broker_client = create_broker_client(settings)
        app.state.check_executor = health.create_check_executor()
        session_factory = create_session_factory(app.state.db_engine)
        storage = LocalStorage(settings.storage_root)
        app.state.upload_service = UploadService(
            session_factory,
            storage,
            UploadLimits(
                pdf_bytes=settings.max_pdf_upload_bytes, zip_bytes=settings.max_zip_upload_bytes
            ),
            ttl=timedelta(hours=settings.upload_ttl_hours),
        )
        app.state.job_service = JobService(
            session_factory, fault_injection_enabled=settings.enable_fault_injection
        )
        if settings.enable_fault_injection:
            logger.warning(
                "fault injection is ENABLED: jobs may request injected failures "
                "(development only; set ENABLE_FAULT_INJECTION=false to turn it off)"
            )
        app.state.artifact_service = ArtifactService(session_factory, storage)
        app.state.event_service = EventService(session_factory)
        app.state.sse_slots = events.StreamSlots(settings.sse_max_streams)
        try:
            yield
        finally:
            # Don't let shutdown/reload wait on a slow health check.
            app.state.check_executor.shutdown(wait=False, cancel_futures=True)
            app.state.broker_client.close()
            app.state.db_engine.dispose()

    app = FastAPI(title="Tasklane", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings

    # Middleware order matters: the last one added runs first. Request IDs must be outermost so
    # every response (including errors) carries one.
    install_error_handling(
        app,
        max_json_body_bytes=settings.max_json_body_bytes,
        exempt_paths={uploads.UPLOADS_PATH},
    )
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(uploads.router)
    app.include_router(jobs.router)
    app.include_router(events.router)
    return app
