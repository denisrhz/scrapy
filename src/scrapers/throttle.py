from __future__ import annotations

import asyncio
from types import TracebackType


class Throttle:
    """Shared HTTP request limiter for the whole run.

    Combines two independent things:

    * `concurrency` — how many requests can be in flight at once;
    * `rps` — how often requests are actually released (token bucket).

    Waiting for a token happens before grabbing a slot, so a slow pace
    doesn't tie up slots with idle tasks — unlike `sleep()` inside a
    semaphore, where the slot stays held for the whole pause.
    """

    def __init__(self, rps: float = 8.0, concurrency: int = 12) -> None:
        if rps <= 0:
            raise ValueError("rps must be greater than zero")
        if concurrency < 1:
            raise ValueError("concurrency must be greater than zero")

        self.rps = rps
        self.concurrency = concurrency
        self._semaphore = asyncio.Semaphore(concurrency)
        # The moment the next request is "reserved" for. Each call pushes it
        # forward by 1/rps, so the queue spreads out evenly.
        self._next_slot: float | None = None
        self._slot_lock = asyncio.Lock()
        self._penalty_until = 0.0
        self._penalty_factor = 1.0
        self._requests = 0
        self._started_at: float | None = None

    # ------------------------------------------------------------------ entry

    async def __aenter__(self) -> Throttle:
        await self._wait_for_slot()
        await self._semaphore.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._semaphore.release()

    async def _wait_for_slot(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._slot_lock:
            now = loop.time()
            interval = 1.0 / self.rps
            if now < self._penalty_until:
                interval *= self._penalty_factor
            slot = max(now, self._next_slot if self._next_slot is not None else now)
            self._next_slot = slot + interval
            delay = slot - now
        if delay > 0:
            await asyncio.sleep(delay)
        # Count the request as released only now: counting it at reservation
        # time would make the stats run ahead by the whole queue length.
        if self._started_at is None:
            self._started_at = loop.time()
        self._requests += 1

    # ----------------------------------------------------------------- feedback

    def penalize(self, status_code: int, seconds: float = 60.0, factor: float = 2.0) -> None:
        """Slows down the pace for `seconds` after a server rejection (429/503).

        Called by the scraper: the retry itself is still its job, the
        throttle just slows the whole queue so retries don't hit the same wall.
        """
        if status_code not in {429, 503}:
            return
        loop = asyncio.get_running_loop()
        self._penalty_factor = factor
        self._penalty_until = max(self._penalty_until, loop.time() + seconds)
        print(f"🐢 Slowing down to {self.rps / factor:.1f} req/s for {seconds:.0f}s (HTTP {status_code})")

    # -------------------------------------------------------------------- stats

    def stats(self) -> tuple[int, float]:
        """Returns (requests released, average req/s)."""
        if self._started_at is None:
            return 0, 0.0
        try:
            elapsed = asyncio.get_running_loop().time() - self._started_at
        except RuntimeError:
            return self._requests, 0.0
        if elapsed <= 0:
            return self._requests, 0.0
        return self._requests, self._requests / elapsed
