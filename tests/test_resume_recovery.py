"""Recovery must honor the desired state while retaining one lifecycle owner."""
import asyncio
import importlib.util
import logging
from pathlib import Path
import sys
import threading
import types

import pytest


@pytest.fixture
def backend(monkeypatch):
    decky = types.ModuleType("decky")
    decky.logger = logging.getLogger("ally-resume-test")
    decky.DECKY_PLUGIN_VERSION = "test"
    events = []

    async def emit(*args):
        events.append(args)

    decky.emit = emit
    monkeypatch.setitem(sys.modules, "decky", decky)
    path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("ally_resume_main", path)
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    return main, events


def make_plugin(main, monkeypatch, *, enabled=True, healthy=False, available=True):
    plugin = main.Plugin()
    plugin.settings = main.settings._merge(main.settings.DEFAULTS, {
        "enabled": enabled, "engine": {"kind": "original"}, "setup": {"done": True},
        "original": {"outputGainDb": 6.0},
    })
    plugin.running_app = None
    plugin.setup_task = plugin.convert_task = None
    plugin.engine_lock = asyncio.Lock()
    plugin.jack = types.SimpleNamespace(headphones=False, paused=False)
    health = {"healthy": healthy, "available": available,
              "reason": "healthy" if healthy else "worker inactive"}
    calls = []
    monkeypatch.setattr(main.dsp_runtime, "engine_kind", lambda: plugin.settings["engine"]["kind"])
    monkeypatch.setattr(main.original_runtime, "health", lambda: dict(health))

    async def apply(force_restart=False):
        assert plugin.engine_lock.locked()
        calls.append(("apply", force_restart))
        health["healthy"] = True
        return {"verified": True}

    async def state():
        return {"active": health["healthy"], "verified": health["healthy"],
                "enabled_setting": plugin.settings["enabled"]}

    plugin._apply_current_locked = apply
    plugin._dsp_state = state
    return plugin, health, calls


def test_same_speaker_state_recovers_stopped_original_worker(backend, monkeypatch):
    main, _ = backend

    async def scenario():
        plugin, _, calls = make_plugin(main, monkeypatch)
        await plugin._recover_audio(False)
        assert calls == [("apply", True)]
        assert plugin.jack.headphones is False
        assert plugin.settings["enabled"] is True
        assert plugin.settings["original"]["outputGainDb"] == 6.0

    asyncio.run(scenario())


def test_healthy_normal_polls_do_not_restart_audio(backend, monkeypatch):
    main, events = backend

    async def scenario():
        plugin, _, calls = make_plugin(main, monkeypatch, healthy=True)
        for _ in range(3):
            await plugin._recover_audio(False)
        assert calls == []
        assert events == []

    asyncio.run(scenario())


def test_resume_rebuilds_even_a_pre_suspend_ready_session(backend, monkeypatch):
    main, _ = backend

    async def scenario():
        plugin, _, calls = make_plugin(main, monkeypatch, healthy=True)
        await plugin._recover_audio(True)
        assert calls == [("apply", True)]

    asyncio.run(scenario())


@pytest.mark.parametrize("resumed", [False, True])
def test_off_setting_is_preserved_across_recovery_and_resume(backend, monkeypatch, resumed):
    main, _ = backend

    async def scenario():
        plugin, _, calls = make_plugin(main, monkeypatch, enabled=False)
        await plugin._recover_audio(resumed)
        assert calls == []
        assert plugin.settings["enabled"] is False

    asyncio.run(scenario())


def test_recovery_rechecks_off_after_waiting_for_lifecycle_lock(backend, monkeypatch):
    main, _ = backend

    async def scenario():
        plugin, _, calls = make_plugin(main, monkeypatch)
        await plugin.engine_lock.acquire()
        task = asyncio.create_task(plugin._recover_audio(True))
        await asyncio.sleep(0)
        plugin.settings["enabled"] = False
        plugin.engine_lock.release()
        await task
        assert calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("busy_task", ["setup_task", "convert_task"])
def test_recovery_does_not_compete_with_setup_or_conversion(backend, monkeypatch, busy_task):
    main, _ = backend

    async def scenario():
        plugin, _, calls = make_plugin(main, monkeypatch)
        future = asyncio.get_running_loop().create_future()
        setattr(plugin, busy_task, future)
        try:
            await plugin._recover_audio(True)
            assert calls == []
        finally:
            future.cancel()

    asyncio.run(scenario())


def test_converted_headphone_pause_remains_paused_after_resume(backend, monkeypatch):
    main, _ = backend

    async def scenario():
        plugin, _, calls = make_plugin(main, monkeypatch)
        plugin.settings["engine"]["kind"] = "converted"
        plugin.jack.paused = True
        plugin.jack.headphones = True
        monkeypatch.setattr(main.original_runtime, "health",
                            lambda: pytest.fail("Converted pause must not inspect original assets"))
        assert await plugin._recover_audio(True) is True
        assert calls == []

    asyncio.run(scenario())


def test_startup_reconcile_finishes_before_periodic_recovery_begins(backend, monkeypatch):
    main, _ = backend

    async def scenario():
        plugin, _, calls = make_plugin(main, monkeypatch)
        plugin._recovery_ready = False
        assert await plugin._recover_audio(True) is False
        assert calls == []
        plugin._recovery_ready = True
        assert await plugin._recover_audio(True) is True
        assert calls == [("apply", True)]

    asyncio.run(scenario())


def test_missing_output_waits_and_recovers_when_output_returns(backend, monkeypatch):
    main, _ = backend

    async def scenario():
        plugin, health, calls = make_plugin(main, monkeypatch, available=False)
        await plugin._recover_audio(True)
        assert calls == []
        assert plugin.settings["enabled"] is True
        health["available"] = True
        await plugin._recover_audio(False)
        assert calls == [("apply", True)]

    asyncio.run(scenario())


def test_failed_start_can_be_retried_without_toggling_saved_enabled(backend, monkeypatch):
    main, events = backend

    async def scenario():
        plugin, _, calls = make_plugin(main, monkeypatch)
        good_apply = plugin._apply_current_locked

        async def failing_apply(force_restart=False):
            raise RuntimeError("ALSA output still negotiating")

        plugin._apply_current_locked = failing_apply
        with pytest.raises(RuntimeError, match="still negotiating"):
            await plugin._recover_audio(False)
        assert plugin.settings["enabled"] is True
        assert events and events[-1][0] == "dsp_state"
        assert events[-1][1]["verified"] is False
        plugin._apply_current_locked = good_apply
        await plugin._recover_audio(False)
        assert calls == [("apply", True)]

    asyncio.run(scenario())


def test_cancellation_waits_for_mutating_recovery_before_releasing_lock(backend, monkeypatch):
    main, _ = backend

    async def scenario():
        plugin, _, _ = make_plugin(main, monkeypatch)
        started, finish = threading.Event(), threading.Event()

        def mutate():
            started.set()
            assert finish.wait(timeout=2)
            assert plugin.engine_lock.locked()

        async def apply(force_restart=False):
            await asyncio.to_thread(mutate)
            return {"verified": True}

        plugin._apply_current_locked = apply
        task = asyncio.create_task(plugin._recover_audio(True))
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(.005)
            assert started.is_set()
            task.cancel()
            await asyncio.sleep(0)
            assert plugin.engine_lock.locked() and not task.done()
            task.cancel()
            await asyncio.sleep(0)
            assert plugin.engine_lock.locked() and not task.done()
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not plugin.engine_lock.locked()

    asyncio.run(scenario())


def test_cancelled_recovery_failure_cannot_restart_the_watcher(backend, monkeypatch):
    main, _ = backend

    async def scenario():
        plugin, _, calls = make_plugin(main, monkeypatch)
        started, finish = threading.Event(), threading.Event()

        def failing_mutation():
            started.set()
            assert finish.wait(timeout=2)
            assert plugin.engine_lock.locked()
            raise RuntimeError("Output disappeared while stopping")

        async def apply(force_restart=False):
            calls.append(("apply", force_restart))
            await asyncio.to_thread(failing_mutation)

        plugin._apply_current_locked = apply
        watcher = main.RecoveryWatcher(plugin._recover_audio, interval=.005,
                                       retry_initial=.005, retry_max=.005)
        watcher.start()
        owned = watcher._task
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(.005)
            assert started.is_set()
            watcher.cancel()
            await asyncio.sleep(0)
            assert plugin.engine_lock.locked() and not owned.done()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(owned), timeout=1)
            assert not plugin.engine_lock.locked()
            await asyncio.sleep(.03)
            assert calls == [("apply", True)]
        finally:
            finish.set()
            watcher.cancel()
            await asyncio.gather(owned, return_exceptions=True)

    asyncio.run(scenario())


def test_cancelled_master_sync_failure_preserves_cancellation(backend, monkeypatch):
    main, _ = backend

    async def scenario():
        plugin, _, _ = make_plugin(main, monkeypatch)
        started, finish = threading.Event(), threading.Event()

        def failing_sync():
            started.set()
            assert finish.wait(timeout=2)
            assert plugin.engine_lock.locked()
            raise RuntimeError("Pulse session disconnected")

        monkeypatch.setattr(main.original_runtime, "sync_master", failing_sync)
        task = asyncio.create_task(plugin._sync_original_master())
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(.005)
            assert started.is_set()
            task.cancel()
            await asyncio.sleep(0)
            assert plugin.engine_lock.locked() and not task.done()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=1)
            assert not plugin.engine_lock.locked()
        finally:
            finish.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
