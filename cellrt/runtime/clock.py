"""Time for the core. In mock mode it is simulated and only moves when the world steps."""

from __future__ import annotations

import asyncio
import heapq
from typing import Protocol

_NS_PER_S = 1_000_000_000


class Clock(Protocol):
    def now(self) -> float:
        """Current time in seconds."""
        ...

    async def sleep(self, dt: float) -> None:
        """Wait `dt` seconds of this clock's time."""
        ...


class SimClock:
    """Simulated clock: `sleep` waits until `advance` has moved time past the deadline.

    Time is kept in integer nanoseconds so that adding 0.05 s twenty times is exactly 1 s.
    """

    def __init__(self) -> None:
        self._now_ns = 0
        self._seq = 0
        self._waiters: list[tuple[int, int, asyncio.Future[None]]] = []

    def now(self) -> float:
        return self._now_ns / _NS_PER_S

    async def sleep(self, dt: float) -> None:
        if dt < 0:
            raise ValueError(f"cannot sleep a negative duration: {dt}")
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._waiters, (self._now_ns + _to_ns(dt), self._seq, future))
        await future

    def advance(self, dt: float) -> None:
        if dt < 0:
            raise ValueError(f"cannot advance by a negative duration: {dt}")
        self._now_ns += _to_ns(dt)
        while self._waiters and self._waiters[0][0] <= self._now_ns:
            _, _, future = heapq.heappop(self._waiters)
            if not future.done():
                future.set_result(None)

    def pending_waiters(self) -> int:
        """Sleeps still waiting (cancelled ones excluded)."""
        return sum(1 for _, _, future in self._waiters if not future.done())


def _to_ns(seconds: float) -> int:
    return round(seconds * _NS_PER_S)
