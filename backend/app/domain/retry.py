"""How long a job waits before an automatic retry (pure).

Exponential backoff with "equal jitter":

    d     = min(base * 2 ** (attempt_no - 1), maximum)
    delay = d / 2 + random(0, d / 2)

With the defaults (base 5 s, maximum 300 s) the first retry is 2.5-5 s away, the second 5-10 s,
the third 10-20 s, and so on, never more than 300 s. The delay is always at least half of `d`
(so retries really back off), and the random half spreads out jobs that failed at the same moment
(so they don't all hit a recovering provider at the same instant).
"""

import random as _random
from collections.abc import Callable
from dataclasses import dataclass

# min() keeps the exponent small even for absurd attempt numbers.
_MAX_EXPONENT = 30


def retry_delay_seconds(
    attempt_no: int,
    *,
    base_seconds: float,
    max_seconds: float,
    random: Callable[[], float] = _random.random,
) -> float:
    """Delay before the retry that follows failed attempt number `attempt_no` (1, 2, ...).

    `random` returns a float in [0, 1); tests pass a fixed function to make the result exact.
    """
    if attempt_no < 1:
        raise ValueError("attempt_no starts at 1")
    if base_seconds <= 0 or max_seconds < base_seconds:
        raise ValueError("need 0 < base_seconds <= max_seconds")
    ceiling = min(base_seconds * 2 ** min(attempt_no - 1, _MAX_EXPONENT), max_seconds)
    half = ceiling / 2
    return half + random() * half


@dataclass(frozen=True)
class RetryPolicy:
    """The backoff settings a worker uses (RETRY_BASE_SECONDS, RETRY_MAX_SECONDS)."""

    base_seconds: float = 5.0
    max_seconds: float = 300.0
    random: Callable[[], float] = _random.random

    def delay_seconds(self, attempt_no: int) -> float:
        return retry_delay_seconds(
            attempt_no,
            base_seconds=self.base_seconds,
            max_seconds=self.max_seconds,
            random=self.random,
        )
