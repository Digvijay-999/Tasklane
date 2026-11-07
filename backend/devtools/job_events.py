"""Print a job's event history: a development helper until the event stream (SSE) of Phase 8.

    docker compose exec -T api python -m devtools.job_events <job_id>          (progress hidden)
    docker compose exec -T api python -m devtools.job_events <job_id> --all    (everything)

Reads the job_events table of the database the api container is configured for. Read-only.
"""

import argparse
import sys
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import JobEvent
from app.db.session import create_db_engine

HIDDEN_BY_DEFAULT = {"stage.progress"}


def describe(event_type: str, payload: Mapping[str, Any]) -> str:
    """One readable line of details for an event (ASCII only, for any console)."""
    if event_type == "job.status_changed":
        text = f"{payload.get('from')} -> {payload.get('to')}"
        if "retry_in_seconds" in payload:
            text += f"  (retry in {payload['retry_in_seconds']:.2f} s)"
        if "gave_up_after_attempts" in payload:
            text += f"  (gave up after {payload['gave_up_after_attempts']} attempts)"
        if payload.get("error_class"):
            text += f"  [{payload['error_class']}]"
        return text
    if event_type == "attempt.started":
        return f"trigger={payload.get('trigger')}"
    if event_type == "stage.failed":
        kind = "retryable" if payload.get("retryable") else "permanent"
        return f"{payload.get('stage')}: {payload.get('error_class')} ({kind})"
    if event_type == "stage.skipped":
        return f"{payload.get('stage')} ({payload.get('reason')})"
    if event_type == "stage.progress":
        return f"{payload.get('stage')} {payload.get('percent')}%"
    if event_type.startswith("stage."):
        return str(payload.get("stage", ""))
    if event_type == "job.cancel_superseded":
        return f"{payload.get('outcome')} ({payload.get('reason')})"
    return ""


def format_rows(events: Iterable[Any], *, show_all: bool = False) -> list[str]:
    lines = [f"{'seq':>4}  {'attempt':>7}  {'event':<20}  details"]
    for event in events:
        if not show_all and event.event_type in HIDDEN_BY_DEFAULT:
            continue
        attempt = "-" if event.attempt_no is None else str(event.attempt_no)
        details = describe(event.event_type, event.payload or {})
        lines.append(f"{event.seq:>4}  {attempt:>7}  {event.event_type:<20}  {details}".rstrip())
    return lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Print a job's event history.")
    parser.add_argument("job_id", type=uuid.UUID)
    parser.add_argument("--all", action="store_true", help="include stage.progress events")
    args = parser.parse_args(argv)

    engine = create_db_engine(get_settings())
    try:
        with Session(engine) as session:
            events = list(
                session.scalars(
                    select(JobEvent).where(JobEvent.job_id == args.job_id).order_by(JobEvent.seq)
                )
            )
    finally:
        engine.dispose()
    if not events:
        print(f"no events for job {args.job_id}", file=sys.stderr)
        return 1
    print("\n".join(format_rows(events, show_all=args.all)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
