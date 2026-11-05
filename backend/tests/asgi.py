"""Call the app through raw ASGI, controlling exactly what the "client" sends.

TestClient sends a whole body at once. These helpers feed the body chunk by chunk, record how
many chunks the server actually read, and can simulate a client disconnecting, or the request
being cancelled, mid-upload.
"""

import json
from dataclasses import dataclass
from typing import Any

import anyio


@dataclass
class AsgiResult:
    status: int | None  # None when no response was sent (e.g. the request was cancelled)
    body: Any
    chunks_read: int


def post_chunks(
    app: Any,
    path: str,
    *,
    query: str = "",
    headers: dict[str, str] | None = None,
    chunks: list[bytes],
    disconnect_after: int | None = None,
    cancel_after: int | None = None,
    timeout: float = 10.0,
) -> AsgiResult:
    """POST `chunks` to `path`.

    - disconnect_after=N: the client "hangs up" after sending N chunks.
    - cancel_after=N: the request is cancelled (like a server shutdown) after N chunks.
    The app must already have run its startup (e.g. inside a TestClient). The whole call fails
    after `timeout` seconds instead of hanging the test suite.
    """
    chunks_read = 0
    messages: list[dict[str, Any]] = []
    cancel_scope: anyio.CancelScope | None = None  # created inside the event loop, in run()

    async def receive() -> dict[str, Any]:
        nonlocal chunks_read
        if cancel_after is not None and chunks_read >= cancel_after and cancel_scope:
            cancel_scope.cancel()
            await anyio.sleep(0)  # a checkpoint where the cancellation is delivered
        if disconnect_after is not None and chunks_read >= disconnect_after:
            return {"type": "http.disconnect"}
        if chunks_read < len(chunks):
            chunk = chunks[chunks_read]
            chunks_read += 1
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": chunks_read < len(chunks),
            }
        await anyio.sleep_forever()  # body complete; nothing more will arrive
        raise AssertionError("unreachable")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }

    async def run() -> None:
        nonlocal cancel_scope
        with anyio.fail_after(timeout), anyio.CancelScope() as cancel_scope:
            await app(scope, receive, send)

    anyio.run(run)
    start = next((m for m in messages if m["type"] == "http.response.start"), None)
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return AsgiResult(
        status=start["status"] if start else None,
        body=json.loads(raw) if raw else None,
        chunks_read=chunks_read,
    )
