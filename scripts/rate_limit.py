"""Shared sliding-window rate limiter for the data.gov.uk CKAN API.

Used by fetch_organisations.py, query_datasets.py and download_datasets.py
— all three hit https://www.data.gov.uk/api/3/action, which allows 4
requests per second. Each script creates exactly one limiter and blocks on
a slot before every API call, so the limiter sees the full stream of
requests.

Same sliding-window semantics as the async version: a window of call
timestamps pruned to the last second; when the window is full the caller
sleeps until the oldest slot falls out. A lock guards the window so
threads can share one limiter safely.
"""

import threading
import time


def create_rate_limiter(max_per_second: int):
    """Create a limiter that allows max_per_second calls per second (sliding window).

    Returns a sync callable that blocks until a slot is available, then
    records the slot and returns. Keeps a sliding window of call
    timestamps, pruned to the last second; when the window is full it
    sleeps until the oldest slot falls out.
    """

    window: list[float] = []
    lock = threading.Lock()

    def wait_for_slot() -> None:
        while True:
            with lock:
                now = time.monotonic()
                while window and window[0] <= now - 1.0:
                    window.pop(0)

                if len(window) < max_per_second:
                    window.append(time.monotonic())
                    return

                # Window is full — sleep until the oldest slot ages out.
                # Compute the delay under the lock but sleep outside it, so
                # another thread can take a slot (or the oldest slot can age
                # out naturally) while we wait.
                delay = window[0] + 1.0 - now
            time.sleep(delay)

    return wait_for_slot


def sleep(ms: float) -> None:
    """Sleep for ms milliseconds (used for 429 backoff)."""
    time.sleep(ms / 1000)
