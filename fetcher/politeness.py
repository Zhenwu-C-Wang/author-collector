"""Politeness controls: per-domain delay and global concurrency."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator


class PolitenessController:
    """Enforce per-domain delay and global concurrency for fetches."""

    def __init__(
        self,
        per_domain_delay_seconds: float,
        max_global_concurrency: int,
        sleep_fn: Callable[[float], None] | None = None,
        clock_fn: Callable[[], float] | None = None,
    ) -> None:
        """Initialize delay/concurrency policy with optional test-time clock hooks."""
        if per_domain_delay_seconds < 0:
            raise ValueError("per_domain_delay_seconds must be >= 0")
        if max_global_concurrency < 1:
            raise ValueError("max_global_concurrency must be >= 1")

        self.per_domain_delay_seconds = per_domain_delay_seconds
        self.max_global_concurrency = max_global_concurrency

        self._sleep = sleep_fn or time.sleep
        self._clock = clock_fn or time.monotonic

        self._semaphore = threading.Semaphore(max_global_concurrency)
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}

    def wait_for_domain(self, domain: str, delay_multiplier: float = 1.0) -> None:
        """Block until this domain is eligible for the next request."""
        effective_multiplier = max(delay_multiplier, 0.0)

        while True:
            with self._lock:
                now = self._clock()
                next_allowed = self._next_allowed.get(domain, now)
                if now >= next_allowed:
                    delay = self.per_domain_delay_seconds * effective_multiplier
                    self._next_allowed[domain] = now + delay
                    return
                wait_seconds = next_allowed - now

            self._sleep(wait_seconds)

    @contextmanager
    def request_slot(self, domain: str, delay_multiplier: float = 1.0) -> Iterator[None]:
        """Acquire global slot and respect per-domain delay."""
        self._semaphore.acquire()
        try:
            self.wait_for_domain(domain, delay_multiplier=delay_multiplier)
            yield
        finally:
            self._semaphore.release()
