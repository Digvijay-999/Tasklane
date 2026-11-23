"""A fake OpenAI-compatible chat-completions server for tests (no network, no API key).

    with FakeProviderServer() as fake:
        fake.reply(Reply(status=429, headers={"Retry-After": "7"}))   # next request
        ... PROVIDER_BASE_URL = fake.base_url ...
        fake.requests  # every request: path, headers, parsed JSON body

Unscripted requests get a normal completion with usage. It listens on 127.0.0.1 on a free port,
so the worker processes started by the e2e tests (same container) can reach it too.
"""

import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

USAGE = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}


def completion(content: str | None, usage: dict | None = USAGE, **message: Any) -> dict:
    body: dict[str, Any] = {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": "fake-resolved-model-2026",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, **message},
                "finish_reason": "stop",
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return body


@dataclass
class Reply:
    status: int = 200
    body: dict | bytes | None = None  # None: a normal completion
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0  # seconds before answering
    trickle: float | None = None  # seconds between single bytes of the body


@dataclass
class Request:
    path: str
    headers: dict[str, str]
    body: dict

    @property
    def user_message(self) -> str:
        return self.body["messages"][1]["content"]


class FakeProviderServer:
    def __init__(self) -> None:
        self.requests: list[Request] = []
        self._replies: deque[Reply] = deque()
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # keep test output quiet
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                with fake._lock:
                    fake.requests.append(Request(self.path, dict(self.headers), body))
                    reply = fake._replies.popleft() if fake._replies else Reply()
                if reply.delay:
                    time.sleep(reply.delay)
                if reply.body is None:
                    digest = hashlib.sha256(json.dumps(body).encode()).hexdigest()[:8]
                    payload = json.dumps(completion(f"Fake summary {digest}.")).encode()
                elif isinstance(reply.body, bytes):
                    payload = reply.body
                else:
                    payload = json.dumps(reply.body).encode()
                try:
                    self.send_response(reply.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    for name, value in reply.headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    if reply.trickle is None:
                        self.wfile.write(payload)
                    else:
                        for i in range(len(payload)):
                            self.wfile.write(payload[i : i + 1])
                            self.wfile.flush()
                            time.sleep(reply.trickle)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client gave up (a timeout test)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def reply(self, *replies: Reply) -> None:
        with self._lock:
            self._replies.extend(replies)

    def __enter__(self) -> "FakeProviderServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
