"""Reconcile desired audio state after suspend and runtime failure.

Linux MONOTONIC excludes suspend while BOOTTIME includes it. Observe their
difference so a resumed worker is rebuilt even if its old READY file survives.
The callback owns locking and every mutation; this watcher only schedules it.
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Awaitable, Callable, Optional

from .log import logger


def suspend_elapsed() -> float:
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    if clock is None:
        return 0.0
    try:
        return time.clock_gettime(clock) - time.monotonic()
    except (OSError, ValueError):
        return 0.0


class RecoveryWatcher:
    def __init__(self, on_check: Callable[[bool], Awaitable[bool]], *,
                 interval: float = 3.0, retry_initial: float = 3.0,
                 retry_max: float = 30.0, clock=time.monotonic,
                 suspend_clock=suspend_elapsed):
        if not 0 < interval or not 0 < retry_initial <= retry_max:
            raise ValueError("Recovery intervals must be positive")
        self.on_check = on_check
        self.interval = interval
        self.retry_initial = retry_initial
        self.retry_max = retry_max
        self.clock = clock
        self.suspend_clock = suspend_clock
        self._suspend_total = suspend_clock()
        self._resume_pending = False
        self._failures = 0
        self._retry_at = 0.0
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_event_loop().create_task(self.run())

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def poll(self) -> bool:
        total = self.suspend_clock()
        if math.isfinite(total):
            if math.isfinite(self._suspend_total) and total - self._suspend_total > 1.0:
                self._resume_pending = True
                self._retry_at = 0.0
                self._failures = 0
            self._suspend_total = total
        now = self.clock()
        if now < self._retry_at:
            return False
        try:
            healthy = bool(await self.on_check(self._resume_pending))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("audio recovery: %s", error)
            healthy = False
        if healthy:
            self._failures = 0
            self._retry_at = 0.0
            self._resume_pending = False
        else:
            delay = min(self.retry_max, self.retry_initial * 2 ** min(self._failures, 10))
            self._failures += 1
            self._retry_at = self.clock() + delay
        return healthy

    async def run(self) -> None:
        while True:
            await self.poll()
            await asyncio.sleep(self.interval)
