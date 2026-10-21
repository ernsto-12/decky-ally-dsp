"""Mirror original-engine master controls after Pulse sink changes.

The subscriber only observes events. The callback owns engine gating, locking
and every audio-session mutation; stopping this watcher leaves audio running.
"""
from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable, Optional

from .log import logger
from .util import user_env


_SINK_EVENT = re.compile(rb"^Event '(?:new|change|remove)' on sink #[0-9]+\s*$")


def is_sink_event(line: bytes) -> bool:
    return bool(_SINK_EVENT.fullmatch(line))


class MasterWatcher:
    def __init__(self, on_change: Callable[[], Awaitable[None]], *,
                 debounce: float = 0.1, restart_delay: float = 1.0):
        self.on_change = on_change
        self.debounce = debounce
        self.restart_delay = restart_delay
        self._task: Optional[asyncio.Task] = None
        self._dispatch_task: Optional[asyncio.Task] = None
        self._process = None
        self._pending = asyncio.Event()
        self._stopping = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._pending.clear()
            self._task = asyncio.get_event_loop().create_task(self.run())

    def cancel(self) -> None:
        # Decky's unload hook must not suspend. Signal only the exact child
        # created here; run() reaps it and escalates to kill with a deadline.
        if self._stopping:
            return
        self._stopping = True
        if self._process is not None and self._process.returncode is None:
            try:
                self._process.terminate()
            except ProcessLookupError:
                pass
        # run() is the single owner of dispatcher cancellation: a second
        # cancel could interrupt a callback finishing a mutating worker thread.
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _dispatch(self) -> None:
        while not self._stopping:
            await self._pending.wait()
            self._pending.clear()
            # A fixed coalescing window cannot be postponed indefinitely by
            # repeated volume keys or by the mirror's own Pulse change events.
            await asyncio.sleep(self.debounce)
            self._pending.clear()
            if self._stopping:
                return
            try:
                await self.on_change()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("master volume sync: %s", error)

    async def _reap(self, process) -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                logger.warning("master volume subscriber did not exit after kill")

    async def run(self) -> None:
        self._dispatch_task = asyncio.create_task(self._dispatch())
        warned = False
        try:
            while not self._stopping:
                process = None
                try:
                    creating = asyncio.create_task(asyncio.create_subprocess_exec(
                        "pactl", "subscribe", stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        env=dict(user_env(), LANG="C", LC_ALL="C")))
                    try:
                        process = await asyncio.shield(creating)
                    except asyncio.CancelledError:
                        # The OS may already have spawned pactl. Acquire its
                        # handle before leaving so finally can reap our child.
                        process = await creating
                        raise
                    self._process = process
                    # Catch changes missed while Decky/Pulse was disconnected.
                    self._pending.set()
                    while not self._stopping:
                        line = await process.stdout.readline()
                        if not line:
                            break
                        if is_sink_event(line):
                            warned = False
                            self._pending.set()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if not warned:
                        logger.warning("master volume subscriber: %s", error)
                        warned = True
                finally:
                    if process is not None:
                        await self._reap(process)
                    self._process = None
                if not self._stopping:
                    await asyncio.sleep(self.restart_delay)
        finally:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
