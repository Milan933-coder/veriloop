"""Circuit breaker, retry policy and Retry-After parsing used by ToolClient (D-12, SPEC §11).

Breaker: `failure_threshold` transient failures within `window_s` -> OPEN for `open_s` -> HALF_OPEN,
where exactly one probe call is let through: success closes the breaker, failure re-opens it.
The clock is injectable so tests can move time without sleeping.
"""

from __future__ import annotations

import random
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from enum import StrEnum

Clock = Callable[[], float]


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        window_s: float = 60.0,
        open_s: float = 30.0,
        clock: Clock = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.window_s = window_s
        self.open_s = open_s
        self._clock = clock
        self._failures: deque[float] = deque()
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock() - self._opened_at >= self.open_s:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def retry_in_s(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self._opened_at + self.open_s - self._clock())

    def allow(self) -> bool:
        """May a call proceed now? In HALF_OPEN only a single in-flight probe is allowed."""
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.OPEN:
            return False
        if self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        self._failures.clear()
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        now = self._clock()
        if self._opened_at is not None:  # failed half-open probe (or late failure): re-open
            self._opened_at = now
            self._probe_in_flight = False
            return
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self.window_s:
            self._failures.popleft()
        if len(self._failures) >= self.failure_threshold:
            self._opened_at = now
            self._failures.clear()

    def release_probe(self) -> None:
        """A half-open probe was cancelled without an outcome: let the next call probe instead."""
        self._probe_in_flight = False


@dataclass(frozen=True)
class RetryPolicy:
    base_delay_s: float = 0.5
    factor: float = 2.0
    max_delay_s: float = 8.0
    jitter: float = 0.25  # +0..25% random spread so concurrent retries don't stampede
    max_retry_after_s: float = 15.0  # a longer server-requested wait is not worth blocking a job for

    def delay(
        self,
        retry_number: int,
        *,
        retry_after_s: float | None = None,
        rng: Callable[[], float] = random.random,
    ) -> float | None:
        """Seconds to wait before retry #retry_number (1-based), or None to stop retrying."""
        if retry_after_s is not None and retry_after_s > self.max_retry_after_s:
            return None
        base = min(self.max_delay_s, self.base_delay_s * self.factor ** (retry_number - 1))
        delay = base * (1.0 + self.jitter * rng())
        if retry_after_s is not None:
            delay = max(delay, retry_after_s)
        return delay


def parse_retry_after(headers: Mapping[str, str], *, now: float | None = None) -> float | None:
    """Seconds to wait from `Retry-After` (seconds or HTTP date) or `X-Ratelimit-Reset`.

    aicredits sends X-Ratelimit-Reset in seconds; some APIs send an epoch (s or ms) there instead,
    so large values are interpreted as absolute times.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    now = time.time() if now is None else now
    raw = lowered.get("retry-after")
    if raw:
        raw = raw.strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(raw).timestamp() - now)
            except (TypeError, ValueError):
                pass
    raw = lowered.get("x-ratelimit-reset")
    if raw:
        try:
            value = float(raw.strip())
        except ValueError:
            return None
        if value > 1e12:  # epoch milliseconds
            return max(0.0, value / 1000.0 - now)
        if value > 1e9:  # epoch seconds
            return max(0.0, value - now)
        return max(0.0, value)
    return None
