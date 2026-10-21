"""Pause the chain while headphones use the shared analog sink."""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Optional

from . import dsp_runtime, hardware
from .log import logger


class JackWatcher:
    def __init__(self, interval: float = 3.0, on_change: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 lifecycle_lock=None):
        self.interval = interval
        self.on_change = on_change
        self.headphones: Optional[bool] = None
        self._task: Optional[asyncio.Task] = None
        self.lifecycle_lock = lifecycle_lock

    @property
    def paused(self) -> bool:
        """The chain stays off while headphones use the shared sink, whoever stopped it."""
        return bool(self.headphones and dsp_runtime.pause_for_headphones())

    def state(self) -> Dict[str, Any]:
        return {"headphones": self.headphones, "paused": self.paused}

    def start(self, should_run: Callable[[], bool]) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_event_loop().create_task(self.run(should_run))

    def cancel(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def poll(self, should_run: Callable[[], bool]) -> None:
        if self.lifecycle_lock is None:
            await self._poll(should_run)
        else:
            async with self.lifecycle_lock:
                await self._poll(should_run)

    async def _poll(self, should_run: Callable[[], bool]) -> None:
        dump = await asyncio.to_thread(hardware.pw_dump)
        if not dump:
            return
        hp = hardware.headphones_active(hardware.output_route(dump))
        if hp == self.headphones:
            return
        first = self.headphones is None  # the backend's startup reconcile starts the chain
        self.headphones = hp
        if hp and dsp_runtime.pause_for_headphones():
            if await asyncio.to_thread(dsp_runtime.is_active):
                await asyncio.to_thread(dsp_runtime.stop)
                logger.info("headphones detected: chain paused")
        elif not first and should_run() and not await asyncio.to_thread(dsp_runtime.is_active):
            await asyncio.to_thread(dsp_runtime.start)
            logger.info("headphones removed: chain resumed")
        if self.on_change:
            res = self.on_change(self.state())
            if asyncio.iscoroutine(res):
                await res

    async def run(self, should_run: Callable[[], bool]) -> None:
        while True:
            try:
                await self.poll(should_run)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("jack watcher: %s", e)
            await asyncio.sleep(self.interval)
