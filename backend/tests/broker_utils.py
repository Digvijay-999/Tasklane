"""Read Celery messages straight out of Redis, to check what was actually put on the wire."""

import base64
import json
from dataclasses import dataclass
from typing import Any

import redis


@dataclass
class WireMessage:
    task: str
    kwargs: dict[str, Any]
    headers: dict[str, Any]
    content_type: str


def queued_messages(client: redis.Redis, queue: str) -> list[WireMessage]:
    """Messages waiting in `queue` (kombu's Redis transport stores each as JSON in a list)."""
    messages = []
    for raw in client.lrange(queue, 0, -1):
        envelope = json.loads(raw)
        args, kwargs, _embed = json.loads(base64.b64decode(envelope["body"]))
        assert args == []
        messages.append(
            WireMessage(
                task=envelope["headers"]["task"],
                kwargs=kwargs,
                headers=envelope["headers"],
                content_type=envelope["content-type"],
            )
        )
    return messages
