from veriloop.core.resilience import BreakerState, CircuitBreaker, RetryPolicy, parse_retry_after
from veriloop.core.testing import FakeClock


def test_breaker_opens_after_5_failures_in_window_then_half_open_single_probe() -> None:
    clock = FakeClock()
    br = CircuitBreaker(clock=clock)
    for _ in range(4):
        br.record_failure()
    assert br.state is BreakerState.CLOSED and br.allow()
    br.record_failure()
    assert br.state is BreakerState.OPEN and not br.allow()
    clock.advance(29.9)
    assert br.state is BreakerState.OPEN
    clock.advance(0.2)
    assert br.state is BreakerState.HALF_OPEN
    assert br.allow() is True  # the single probe
    assert br.allow() is False  # everyone else waits for the probe
    br.record_failure()  # probe failed -> re-open for another 30 s
    assert br.state is BreakerState.OPEN
    clock.advance(30)
    assert br.allow()
    br.record_success()
    assert br.state is BreakerState.CLOSED and br.allow() and br.allow()


def test_breaker_window_forgets_old_failures() -> None:
    clock = FakeClock()
    br = CircuitBreaker(clock=clock)
    for _ in range(4):
        br.record_failure()
    clock.advance(61)
    br.record_failure()
    assert br.state is BreakerState.CLOSED


def test_breaker_cancelled_probe_is_released() -> None:
    clock = FakeClock()
    br = CircuitBreaker(clock=clock, failure_threshold=1)
    br.record_failure()
    clock.advance(31)
    assert br.allow() and not br.allow()
    br.release_probe()
    assert br.allow()


def test_retry_delays_exponential_with_bounded_jitter_and_retry_after() -> None:
    p = RetryPolicy(base_delay_s=0.5, factor=2, jitter=0.25)
    assert p.delay(1, rng=lambda: 0.0) == 0.5
    assert p.delay(2, rng=lambda: 0.0) == 1.0
    assert p.delay(2, rng=lambda: 1.0) == 1.25
    assert p.delay(10, rng=lambda: 0.0) == p.max_delay_s
    assert p.delay(1, retry_after_s=3.0, rng=lambda: 0.0) == 3.0
    assert p.delay(1, retry_after_s=60.0) is None  # too long to block a job for


def test_parse_retry_after_variants() -> None:
    assert parse_retry_after({"Retry-After": "7"}) == 7.0
    assert parse_retry_after({"X-Ratelimit-Reset": "12"}) == 12.0
    assert parse_retry_after({"x-ratelimit-reset": "1700000010"}, now=1700000000.0) == 10.0
    assert parse_retry_after({"x-ratelimit-reset": "1700000010000"}, now=1700000000.0) == 10.0
    assert parse_retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, now=1445412470.0) == 10.0
    assert parse_retry_after({}) is None
    assert parse_retry_after({"Retry-After": "soon"}) is None
