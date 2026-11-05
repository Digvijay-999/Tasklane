"""Health routes.

- /health/live answers "is the process running?" and never touches dependencies.
- /health/ready answers "can this API do its job?". Only PostgreSQL is required: it is the source
  of truth for job state, and the API keeps serving that state even when the queue is down. Redis
  is reported for visibility but does not make the API "not ready".
"""

import logging
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel

from app.services.health import DependencyCheck, broker_check, database_check

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["health"])

CheckResult = Literal["ok", "unavailable"]


def create_check_executor() -> ThreadPoolExecutor:
    """Thread pool for dependency checks; created and shut down by the app lifespan.

    Checks run on these threads so we can stop *waiting* for them after a deadline. Driver
    timeouts alone aren't enough: e.g. a DNS lookup for a stopped Docker container can take ~4 s
    to fail, and neither psycopg's nor redis-py's timeouts cover DNS. A check that overruns keeps
    running in the background until its own timeouts end it; the HTTP caller doesn't wait for it.
    """
    return ThreadPoolExecutor(max_workers=8, thread_name_prefix="health-check")


class LiveResponse(BaseModel):
    status: Literal["ok"]


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckResult]


# Dependency providers. Tests replace these with fakes via app.dependency_overrides
# (similar to stubbing a module in Jest).
def get_database_check(request: Request) -> DependencyCheck:
    return database_check(request.app.state.db_engine)


def get_broker_check(request: Request) -> DependencyCheck:
    return broker_check(request.app.state.broker_client)


def get_check_timeout(request: Request) -> float:
    return float(request.app.state.settings.dependency_timeout_seconds)


def get_check_executor(request: Request) -> ThreadPoolExecutor:
    return request.app.state.check_executor


def run_checks(
    checks: dict[str, DependencyCheck], timeout: float, executor: ThreadPoolExecutor
) -> dict[str, CheckResult]:
    """Run all checks concurrently; anything that fails or overruns `timeout` is unavailable."""
    futures = {name: executor.submit(check) for name, check in checks.items()}
    done, _ = wait(futures.values(), timeout=timeout)

    results: dict[str, CheckResult] = {}
    for name, future in futures.items():
        if future not in done:
            # cancel() succeeds only for checks still queued (all threads busy with older,
            # hung checks). Removing them stops a backlog from building up under load.
            error_type = "NotStarted" if future.cancel() else "Timeout"
        elif (exc := future.exception()) is not None:
            # Log the error type, not the message: driver messages can include hosts and users.
            error_type = type(exc).__name__
        else:
            results[name] = "ok"
            continue
        logger.warning(
            "dependency check failed", extra={"dependency": name, "error_type": error_type}
        )
        results[name] = "unavailable"
    return results


@router.get("/live", response_model=LiveResponse)
def live() -> LiveResponse:
    return LiveResponse(status="ok")


# A plain `def` (not `async def`) endpoint: FastAPI runs it in a worker thread, so the blocking
# waits inside it don't freeze the server for other requests.
@router.get(
    "/ready",
    response_model=ReadyResponse,
    responses={503: {"model": ReadyResponse, "description": "A required dependency is down"}},
)
def ready(
    response: Response,
    # Depends(...) tells FastAPI to call the provider and inject its result, a bit like Express
    # middleware that attaches something to `req` before the handler runs.
    database_check: Annotated[DependencyCheck, Depends(get_database_check)],
    broker_check: Annotated[DependencyCheck, Depends(get_broker_check)],
    timeout: Annotated[float, Depends(get_check_timeout)],
    executor: Annotated[ThreadPoolExecutor, Depends(get_check_executor)],
) -> ReadyResponse:
    checks = run_checks({"database": database_check, "queue": broker_check}, timeout, executor)
    if checks["database"] != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(status="not_ready", checks=checks)
    return ReadyResponse(status="ready", checks=checks)
