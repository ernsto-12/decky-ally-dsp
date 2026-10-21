"""Pulse-event control mirroring without touching the host audio session."""
import asyncio
import importlib.util
import logging
from pathlib import Path
import sys
import threading
import types

import pytest

from allydsp import masterwatch


@pytest.mark.parametrize("line,expected", [
    (b"Event 'change' on sink #24\n", True),
    (b"Event 'new' on sink #1\n", True),
    (b"Event 'remove' on sink #123\r\n", True),
    (b"Event 'change' on sink-input #24\n", False),
    (b"Event 'change' on source #24\n", False),
    (b"Event 'change' on server #0\n", False),
    (b"Event 'change' on card #1\n", False),
    (b"Event 'change' on sink #oops\n", False),
    (b"unrelated on sink #24\n", False),
])
def test_only_sink_events(line, expected):
    assert masterwatch.is_sink_event(line) is expected


class Reader:
    def __init__(self):
        self.lines = asyncio.Queue()

    async def readline(self):
        return await self.lines.get()


class Process:
    def __init__(self, *, ignores_terminate=False):
        self.stdout = Reader()
        self.returncode = None
        self.ignores_terminate = ignores_terminate
        self.terminates = 0
        self.kills = 0
        self.exited = asyncio.Event()

    def finish(self):
        self.returncode = 0
        self.exited.set()
        self.stdout.lines.put_nowait(b"")

    def terminate(self):
        self.terminates += 1
        if not self.ignores_terminate:
            self.finish()

    def kill(self):
        self.kills += 1
        self.finish()

    async def wait(self):
        await self.exited.wait()
        return self.returncode


async def until(predicate, timeout=1.0):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), timeout=timeout)


def install_processes(monkeypatch, processes):
    calls = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return processes[len(calls) - 1]

    monkeypatch.setattr(masterwatch.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(masterwatch, "user_env", lambda: {"LANG": "C.UTF-8"})
    return calls


def test_initial_sync_debounce_and_unrelated_events(monkeypatch):
    async def scenario():
        process = Process()
        calls = install_processes(monkeypatch, [process])
        synced = []

        async def sync():
            synced.append(1)

        watcher = masterwatch.MasterWatcher(sync, debounce=0.01)
        watcher.start()
        watcher.start()  # one persistent subscriber, even after a repeated start
        await until(lambda: len(synced) == 1)
        assert len(calls) == 1 and calls[0][0] == ("pactl", "subscribe")
        assert calls[0][1]["env"] == {"LANG": "C", "LC_ALL": "C"}
        assert calls[0][1]["stderr"] == asyncio.subprocess.DEVNULL
        for line in (b"Event 'change' on sink-input #1\n", b"Event 'change' on server #0\n"):
            process.stdout.lines.put_nowait(line)
        await asyncio.sleep(0.03)
        assert len(synced) == 1
        for _ in range(20):
            process.stdout.lines.put_nowait(b"Event 'change' on sink #2\n")
        await until(lambda: len(synced) == 2)
        await asyncio.sleep(0.02)
        assert len(synced) == 2
        watcher.cancel()
        await asyncio.gather(watcher._task, return_exceptions=True)
        assert process.returncode == 0
    asyncio.run(scenario())


def test_reconnects_after_eof_and_syncs_missed_state(monkeypatch):
    async def scenario():
        first, second = Process(), Process()
        calls = install_processes(monkeypatch, [first, second])
        synced = []

        async def sync():
            synced.append(1)

        watcher = masterwatch.MasterWatcher(sync, debounce=0.001, restart_delay=0.02)
        assert masterwatch.MasterWatcher(sync).restart_delay == 1.0
        watcher.start()
        await until(lambda: len(synced) == 1)
        first.finish()
        await asyncio.sleep(0.005)
        assert len(calls) == 1
        await until(lambda: len(synced) == 2)
        assert len(calls) == 2
        watcher.cancel()
        await asyncio.gather(watcher._task, return_exceptions=True)
        assert first.kills == 0 and second.returncode == 0
    asyncio.run(scenario())


def test_cancel_signals_only_owned_subscriber_and_prevents_delayed_sync(monkeypatch):
    async def scenario():
        owned, unrelated = Process(), Process()
        install_processes(monkeypatch, [owned])
        synced = []

        async def sync():
            synced.append(1)

        watcher = masterwatch.MasterWatcher(sync, debounce=0.03)
        watcher.start()
        await until(lambda: watcher._process is owned)
        watcher.cancel()
        watcher.cancel()  # repeated unload must not interrupt cleanup again
        assert owned.terminates == 1  # before yielding back to the event loop
        await asyncio.gather(watcher._task, return_exceptions=True)
        await asyncio.sleep(0.04)
        assert synced == []
        assert unrelated.returncode is None and unrelated.terminates == unrelated.kills == 0
    asyncio.run(scenario())


def test_termination_escalates_for_unresponsive_owned_child(monkeypatch):
    async def scenario():
        process = Process(ignores_terminate=True)
        install_processes(monkeypatch, [process])

        async def sync():
            pass

        watcher = masterwatch.MasterWatcher(sync)
        watcher.start()
        await until(lambda: watcher._process is process)
        watcher.cancel()
        await asyncio.wait_for(asyncio.gather(watcher._task, return_exceptions=True), timeout=1.5)
        assert process.kills == 1 and process.returncode == 0
    asyncio.run(scenario())


def test_callback_failure_does_not_stop_subscription(monkeypatch):
    async def scenario():
        process = Process()
        install_processes(monkeypatch, [process])
        count = []

        async def sync():
            count.append(1)
            if len(count) == 1:
                raise RuntimeError("temporary audio-server failure")

        watcher = masterwatch.MasterWatcher(sync, debounce=0.001)
        watcher.start()
        await until(lambda: len(count) == 1)
        process.stdout.lines.put_nowait(b"Event 'change' on sink #1\n")
        await until(lambda: len(count) == 2)
        assert watcher._task and not watcher._task.done()
        watcher.cancel()
        await asyncio.gather(watcher._task, return_exceptions=True)
    asyncio.run(scenario())


def test_cancel_while_spawn_is_in_flight_still_reaps_owned_child(monkeypatch):
    async def scenario():
        process = Process()
        creating, release = asyncio.Event(), asyncio.Event()

        async def spawn(*args, **kwargs):
            creating.set()
            await release.wait()
            return process

        async def sync():
            raise AssertionError("A stopped subscriber must not sync")

        monkeypatch.setattr(masterwatch.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(masterwatch, "user_env", lambda: {})
        watcher = masterwatch.MasterWatcher(sync)
        watcher.start()
        await creating.wait()
        watcher.cancel()
        await asyncio.sleep(0)
        assert not watcher._task.done()
        release.set()
        await asyncio.gather(watcher._task, return_exceptions=True)
        assert process.terminates == 1 and process.returncode == 0
    asyncio.run(scenario())


def load_main(monkeypatch):
    decky = types.ModuleType("decky")
    decky.logger = logging.getLogger("decky-master-test")
    decky.DECKY_PLUGIN_VERSION = "0.2.2"

    async def emit(*args):
        pass

    decky.emit = emit
    monkeypatch.setitem(sys.modules, "decky", decky)
    path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("ally_master_main", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("kind,enabled,expected", [
    ("original", True, 1), ("original", False, 0), ("converted", True, 0),
])
def test_production_callback_gates_engine_and_enabled_under_lock(monkeypatch, kind, enabled, expected):
    main = load_main(monkeypatch)

    async def scenario():
        plugin = main.Plugin()
        plugin.engine_lock = asyncio.Lock()
        plugin.settings = {"enabled": enabled}
        calls = []
        monkeypatch.setattr(main.dsp_runtime, "engine_kind", lambda: kind)

        def sync():
            assert plugin.engine_lock.locked()
            calls.append(threading.get_ident())

        monkeypatch.setattr(main.original_runtime, "sync_master", sync)
        await plugin._sync_original_master()
        assert len(calls) == expected
        if calls:
            assert calls[0] != threading.get_ident()
    asyncio.run(scenario())


def test_callback_rechecks_gate_after_waiting_for_engine_lock(monkeypatch):
    main = load_main(monkeypatch)

    async def scenario():
        plugin = main.Plugin()
        plugin.engine_lock = asyncio.Lock()
        plugin.settings = {"enabled": True}
        calls = []
        monkeypatch.setattr(main.dsp_runtime, "engine_kind", lambda: "original")
        monkeypatch.setattr(main.original_runtime, "sync_master", lambda: calls.append(1))
        await plugin.engine_lock.acquire()
        task = asyncio.create_task(plugin._sync_original_master())
        await asyncio.sleep(0)
        plugin.settings["enabled"] = False
        plugin.engine_lock.release()
        await task
        assert calls == []
    asyncio.run(scenario())


def test_cancellation_keeps_engine_lock_until_mutating_thread_finishes(monkeypatch):
    main = load_main(monkeypatch)

    async def scenario():
        plugin = main.Plugin()
        plugin.engine_lock = asyncio.Lock()
        plugin.settings = {"enabled": True}
        started, finish = threading.Event(), threading.Event()
        monkeypatch.setattr(main.dsp_runtime, "engine_kind", lambda: "original")

        def sync():
            started.set()
            assert finish.wait(timeout=2)

        monkeypatch.setattr(main.original_runtime, "sync_master", sync)
        task = asyncio.create_task(plugin._sync_original_master())
        await until(started.is_set)
        task.cancel()
        await asyncio.sleep(0)
        assert plugin.engine_lock.locked() and not task.done()
        task.cancel()
        await asyncio.sleep(0)
        assert plugin.engine_lock.locked() and not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not plugin.engine_lock.locked()
    asyncio.run(scenario())
