"""GET /metrics: Prometheus text format (every metric is documented in docs/observability.md).

Bound to 127.0.0.1 like every other port. Not traced (Prometheus scrapes it every 15 s).
"""

from fastapi import APIRouter, Request, Response

from app.observability.metrics import render

router = APIRouter(tags=["observability"])
# The classic text exposition format, understood by every Prometheus version (the client library's
# default advertises 1.0.0, which only newer servers negotiate; the text is the same).
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request) -> Response:
    # A sync route: FastAPI runs it in a worker thread, so the database reads behind the
    # database-derived metrics never block the event loop (and its event streams).
    body = render([request.app.state.database_metrics])
    return Response(body, media_type=CONTENT_TYPE)
