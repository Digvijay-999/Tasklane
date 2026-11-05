"""Backoff for publishing outbox messages when the queue (Redis) is unavailable (pure)."""

DISPATCH_BACKOFF_BASE_SECONDS = 1.0
DISPATCH_BACKOFF_MAX_SECONDS = 60.0


def dispatch_backoff_seconds(failed_attempts: int) -> float:
    """Delay before trying to publish again after `failed_attempts` consecutive failures.

    1 s, 2 s, 4 s, ... capped at 60 s. The dispatcher never gives up: the job stays queued and
    visible, and goes out as soon as Redis is back.
    """
    if failed_attempts <= 0:
        return 0.0
    # min() before the power keeps the exponent small even after thousands of failures.
    exponent = min(failed_attempts - 1, 16)
    return min(DISPATCH_BACKOFF_BASE_SECONDS * 2**exponent, DISPATCH_BACKOFF_MAX_SECONDS)
