"""Connect to a job's event stream (SSE) and print one line per event.

    docker compose exec -T api python -m devtools.sse_client http://localhost:8000/v1/jobs/<id>/events
        --after N            resume after seq N (query parameter, like a refreshed browser page)
        --last-event-id N    resume after seq N (Last-Event-ID header, as EventSource does)
        --stop-after N       disconnect after N printed events (simulates a dropped connection)
        --all                also print stage.progress events
        --raw                print the raw SSE text instead

Exit codes: 0 the server ended the stream with stream.end (job finished); 2 stopped by
--stop-after; 3 the server closed the stream without stream.end (lifetime limit or shutdown:
reconnect with the printed id); 1 an HTTP error. Standard library only.
"""

import argparse
import http.client
import json
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from devtools.job_events import HIDDEN_BY_DEFAULT, describe


@dataclass
class SseMessage:
    """One block of SSE text (up to a blank line). A comment-only block has no event/data."""

    id: str | None = None
    event: str | None = None
    data: str | None = None
    retry: int | None = None
    comments: list[str] = field(default_factory=list)


def iter_sse(lines: Iterable[bytes]) -> Iterator[SseMessage]:
    """Parse SSE lines (as bytes, with their line endings) into messages, per the HTML spec's
    field rules: `name: value` (one optional space after the colon), `:` starts a comment, data
    lines are joined with newlines. A block is yielded at each blank line."""
    message, started = SseMessage(), False
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\n").rstrip("\r")
        if not line:
            if started:
                yield message
            message, started = SseMessage(), False
            continue
        started = True
        if line.startswith(":"):
            message.comments.append(line[1:].strip())
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "data":
            message.data = value if message.data is None else f"{message.data}\n{value}"
        elif name == "event":
            message.event = value
        elif name == "id" and "\0" not in value:
            message.id = value
        elif name == "retry" and value.isdigit():
            message.retry = int(value)


def open_stream(
    url: str, *, after: int | None, last_event_id: int | None, timeout: float
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    """Send the request. Close the returned connection to disconnect (closes the socket)."""
    parts = urlsplit(url)
    connection_class = (
        http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_class(parts.netloc, timeout=timeout)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    if after is not None:
        path += ("&" if "?" in path else "?") + f"after={after}"
    headers = {"Accept": "text/event-stream"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)
    connection.request("GET", path, headers=headers)
    return connection, connection.getresponse()


def event_line(message: SseMessage) -> str:
    item = json.loads(message.data or "{}")
    attempt = "-" if item.get("attempt_no") is None else str(item["attempt_no"])
    details = describe(message.event or "", item.get("payload") or {})
    return f"{message.id:>4}  {attempt:>7}  {message.event:<21}  {details}".rstrip()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Print a job's event stream.")
    parser.add_argument("url", help="e.g. http://localhost:8000/v1/jobs/<job_id>/events")
    parser.add_argument("--after", type=int, help="resume after this seq (query parameter)")
    parser.add_argument("--last-event-id", type=int, help="resume after this seq (header)")
    parser.add_argument("--stop-after", type=int, help="disconnect after N printed events")
    parser.add_argument("--all", action="store_true", help="include stage.progress events")
    parser.add_argument("--raw", action="store_true", help="print the raw SSE text")
    parser.add_argument("--timeout", type=float, default=60, help="read timeout in seconds")
    args = parser.parse_args(argv)

    connection, response = open_stream(
        args.url, after=args.after, last_event_id=args.last_event_id, timeout=args.timeout
    )
    try:
        return _print_stream(response, args)
    finally:
        connection.close()


def _print_stream(response: http.client.HTTPResponse, args: argparse.Namespace) -> int:
    if response.status != 200:
        body = response.read().decode("utf-8", errors="replace")
        try:
            error = json.loads(body)["error"]
            print(f"HTTP {response.status}: {error['code']}: {error['message']}", file=sys.stderr)
        except (ValueError, KeyError, TypeError):
            print(f"HTTP {response.status}: {body[:200]}", file=sys.stderr)
        return 1

    if args.raw:
        ended = False
        for raw in response:
            ended = ended or raw.startswith(b"event: stream.end")
            sys.stdout.write(raw.decode("utf-8"))
            sys.stdout.flush()
        return 0 if ended else 3

    print(f"{'seq':>4}  {'attempt':>7}  {'event':<21}  details", flush=True)
    printed, last_id = 0, args.last_event_id if args.last_event_id is not None else args.after
    for message in iter_sse(response):
        if message.id is not None:
            last_id = message.id
        if message.event == "stream.end":
            end = json.loads(message.data or "{}")
            print(
                f"stream.end: {end.get('reason')} (status {end.get('status')}, last seq "
                f"{end.get('last_seq')}); the server closed the stream",
                flush=True,
            )
            return 0
        if message.event is None or message.data is None:
            continue  # retry field, keepalive comment, or a bare id before a lifetime close
        if args.all or message.event not in HIDDEN_BY_DEFAULT:
            print(event_line(message), flush=True)
            printed += 1
        if args.stop_after is not None and printed >= args.stop_after:
            print(
                f"disconnected after {printed} events; last id {last_id} "
                f"(resume with --last-event-id {last_id})",
                flush=True,
            )
            return 2
    print(
        f"the server closed the stream without stream.end; last id {last_id} "
        f"(resume with --last-event-id {last_id})",
        flush=True,
    )
    return 3


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
