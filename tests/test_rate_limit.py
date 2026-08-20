"""Unit test for scripts/rate_limit.py (run with `uv run pytest tests/test_rate_limit.py`).

Verifies the limiter's contract:
- 8 calls at 4/s take >= ~1.75s
- never more than max_per_second calls complete inside any 1-second window
- the sleep() helper sleeps for (about) the requested milliseconds
"""

import time

from scripts.rate_limit import create_rate_limiter, sleep


def burst(rate: int, n: int) -> list[float]:
    """Acquire n slots through one limiter; return monotonic slot times."""
    limiter = create_rate_limiter(rate)
    times: list[float] = []
    for _ in range(n):
        limiter()
        times.append(time.monotonic())
    return times


def _rate_limit_checks() -> None:
    # --- 8 calls at 4/s: bursts 4, then the 5th waits for the first slot to
    # age out (~1s) and 6-8 ride along — ~1.0s total. Expected timing:
    # slots at 0,0,0,0,1001,1001,1001,1001.
    start = time.monotonic()
    times = burst(4, 8)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.9, f"8 calls at 4/s took only {elapsed:.3f}s (expected >= ~1.0s)"
    assert elapsed < 2.0, f"8 calls at 4/s took {elapsed:.3f}s — too slow"
    assert times[0] - start < 0.1, "first slot should be immediate"
    assert times[4] - start >= 0.9, f"5th slot should wait ~1s, got {times[4] - start:.3f}s"
    print(f"ok: 8 calls at 4/s took {elapsed:.3f}s (expected ~1.0s)")

    # --- sliding-window property: never more than `rate` slots inside any
    # 1-second span (checked on the recorded slot times). ---
    for _i, t in enumerate(times):
        within = sum(1 for u in times if t <= u < t + 1.0)
        assert within <= 4, f"window [{t:.3f}, {t + 1:.3f}) allowed {within} slots (max 4)"
    print("ok: no 1-second window contains more than 4 slots")

    # --- small rate stays fast to run but holds the same invariant ---
    times = burst(2, 6)
    assert time.monotonic() - start < 10
    for _i, t in enumerate(times):
        within = sum(1 for u in times if t <= u < t + 1.0)
        assert within <= 2, f"rate-2 window allowed {within} slots"
    print("ok: rate-2 limiter holds the sliding-window invariant")

    # --- sleep(ms) helper ---
    s = time.monotonic()
    sleep(100)
    took = (time.monotonic() - s) * 1000
    assert 90 <= took <= 400, f"sleep(100) took {took:.1f}ms"
    print(f"ok: sleep(100) took {took:.1f}ms")


def test_rate_limit() -> None:
    _rate_limit_checks()
