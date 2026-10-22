"""Cancelling a Decky RPC must not release ownership of an in-flight mutator."""
import asyncio
import copy
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
    decky.logger = logging.getLogger("dolby-mutation-test")
    decky.DECKY_PLUGIN_VERSION = "test"
    events = []

    async def emit(*event):
        events.append(event)

    decky.emit = emit
    monkeypatch.setitem(sys.modules, "decky", decky)
    spec = importlib.util.spec_from_file_location(
        "ally_dolby_mutation_main", Path(__file__).resolve().parents[1] / "main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    return main, events


class Gate:
    def __init__(self):
        self.started = threading.Event()
        self.finish = threading.Event()

    def block(self, plugin):
        assert plugin.engine_lock.locked()
        self.started.set()
        assert self.finish.wait(timeout=10), "test mutator was never released"
        assert plugin.engine_lock.locked()


async def started(gate):
    for _ in range(1000):
        if gate.started.is_set():
            return
        await asyncio.sleep(.005)
    raise AssertionError("test mutator did not start")


async def cancel_repeatedly(task):
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()


def plugin_for(main, monkeypatch):
    plugin = main.Plugin()
    plugin.engine_lock = asyncio.Lock()
    plugin.settings = {"engine": {"kind": "original"}, "enabled": True,
                       "original": {"outputGainDb": 0, "controls": {"eqEnabled": False}}}
    plugin._save = lambda: None
    plugin._should_run = lambda: True
    plugin.running_app = None
    monkeypatch.setattr(main.dsp_runtime, "engine_kind", lambda: "original")
    monkeypatch.setattr(main.hardware, "codec_info", lambda: {
        "ssid": "10431384", "dev": "0294", "supported": True})

    async def state():
        return {"available": True}

    plugin.get_original_controls = state
    return plugin


@pytest.mark.parametrize("mutation_fails", [False, True])
def test_repeated_cancel_waits_for_control_commit_or_complete_rollback(backend, monkeypatch, mutation_fails):
    main, events = backend

    async def scenario():
        plugin = plugin_for(main, monkeypatch)
        before = copy.deepcopy(plugin.settings)
        apply, rollback = Gate(), Gate()
        calls = []

        def mutate(value):
            calls.append(("apply", value))
            apply.block(plugin)
            if mutation_fails:
                raise RuntimeError("receiver failed after its bounded work")
            return {"applied": True}

        def restore(value):
            calls.append(("rollback", value))
            rollback.block(plugin)
            assert plugin.settings == before

        monkeypatch.setattr(main.original_runtime, "apply_controls", mutate)
        monkeypatch.setattr(main.original_runtime, "_write_controls", restore)
        task = asyncio.create_task(plugin.set_original_controls({"eqEnabled": True}))
        contender = None
        try:
            await started(apply)
            await cancel_repeatedly(task)

            async def compete():
                async with plugin.engine_lock:
                    calls.append(("next-engine-operation",))

            contender = asyncio.create_task(compete())
            await asyncio.sleep(0)
            assert not contender.done()
            apply.finish.set()
            if mutation_fails:
                await started(rollback)
                await cancel_repeatedly(task)
                assert not contender.done()
                rollback.finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
            await contender
            assert not plugin.engine_lock.locked()
            assert [call[0] for call in calls] == (["apply", "rollback", "next-engine-operation"]
                                                  if mutation_fails else ["apply", "next-engine-operation"])
            assert plugin.settings == before if mutation_fails else plugin.settings["original"]["controls"] == {"eqEnabled": True}
            assert bool(events) is (not mutation_fails)
        finally:
            apply.finish.set()
            rollback.finish.set()
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)
            if contender and not contender.done():
                await contender

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation_fails", [False, True])
def test_repeated_cancel_waits_for_profile_upgrade_and_finally_restart(backend, monkeypatch, mutation_fails):
    from allydsp import original_assets, original_profiles
    main, _ = backend

    async def scenario():
        plugin = plugin_for(main, monkeypatch)
        upgrade, restart = Gate(), Gate()
        calls = []
        monkeypatch.setattr(original_profiles, "ensure_profile_xml_sources", lambda: ("speaker.xml", "headphone.xml"))
        monkeypatch.setattr(main.dsp_runtime, "is_active", lambda: True)

        def stop():
            assert plugin.engine_lock.locked()
            calls.append("stop")
            return True

        def import_profiles(**_):
            calls.append("upgrade")
            upgrade.block(plugin)
            if mutation_fails:
                raise RuntimeError("profile import failed before manifest commit")
            return {"ok": True}

        async def restore_audio(force_restart=False):
            assert force_restart
            calls.append("restart")
            await asyncio.to_thread(restart.block, plugin)
            calls.append("restart-complete")

        monkeypatch.setattr(main.dsp_runtime, "stop", stop)
        monkeypatch.setattr(original_assets, "upgrade_engine_profiles", import_profiles)
        plugin._apply_current_locked = restore_audio
        task = asyncio.create_task(plugin.prepare_original_profiles())
        contender = None
        try:
            await started(upgrade)
            await cancel_repeatedly(task)

            async def compete():
                async with plugin.engine_lock:
                    calls.append("next-engine-operation")

            contender = asyncio.create_task(compete())
            await asyncio.sleep(0)
            assert not contender.done()
            upgrade.finish.set()
            await started(restart)
            await cancel_repeatedly(task)
            assert not contender.done()
            restart.finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
            await contender
            assert calls == ["stop", "upgrade", "restart", "restart-complete", "next-engine-operation"]
            assert not plugin.engine_lock.locked()
        finally:
            upgrade.finish.set()
            restart.finish.set()
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)
            if contender and not contender.done():
                await contender

    asyncio.run(scenario())


def test_profile_source_preparation_failure_does_not_stop_playing_audio(backend, monkeypatch):
    from allydsp import original_profiles
    main, _ = backend

    async def scenario():
        plugin = plugin_for(main, monkeypatch)
        def fail_sources():
            raise RuntimeError("pinned ASUS source temporarily unavailable")
        monkeypatch.setattr(original_profiles, "ensure_profile_xml_sources", fail_sources)
        monkeypatch.setattr(main.dsp_runtime, "stop", lambda: pytest.fail("source preparation must precede audio stop"))
        with pytest.raises(RuntimeError, match="ASUS source"):
            await plugin.prepare_original_profiles()
        assert not plugin.engine_lock.locked()
        assert plugin.settings["enabled"] is True

    asyncio.run(scenario())


def test_unsupported_hardware_profile_preparation_never_fetches_or_stops_audio(backend, monkeypatch):
    from allydsp import original_profiles
    main, _ = backend

    async def scenario():
        plugin = plugin_for(main, monkeypatch)
        monkeypatch.setattr(main.hardware, "codec_info", lambda: {
            "ssid": "10431eb3", "dev": "0294", "supported": True})
        monkeypatch.setattr(original_profiles, "ensure_profile_xml_sources",
                            lambda: pytest.fail("unverified model must not fetch RC73XA XML"))
        monkeypatch.setattr(main.dsp_runtime, "stop", lambda: pytest.fail("unverified model must not stop audio"))
        with pytest.raises(RuntimeError, match="RC73XA"):
            await plugin.prepare_original_profiles()
        assert plugin.settings["enabled"] is True
        assert not plugin.engine_lock.locked()

    asyncio.run(scenario())


def test_external_output_keeps_desired_on_and_stores_profile_without_starting_audio(backend, monkeypatch):
    main, events = backend

    async def scenario():
        plugin = plugin_for(main, monkeypatch)
        plugin.settings["global"] = {"profile": "movie", "voicing": "warm"}
        current_default = "alsa_output.usb-dock.analog-stereo"
        calls = []
        monkeypatch.setattr(main.original_runtime, "health", lambda: {
            "healthy": False, "available": False, "reason": "external-output-selected"})
        monkeypatch.setattr(main.dsp_runtime, "stop", lambda: calls.append("stop-owned-engine") or True)
        def select(profile, voicing, pregain, restart_unit):
            assert plugin.engine_lock.locked()
            assert restart_unit is False
            calls.append(("pending-selection", profile, voicing))
            return {"profile": profile, "voicing": voicing, "verified": False}
        monkeypatch.setattr(main.dsp_runtime, "apply_preset", select)
        monkeypatch.setattr(main.dsp_runtime, "start", lambda: pytest.fail("the external output must not be taken over"))
        monkeypatch.setattr(main.original_runtime, "_routing_action", lambda *_: pytest.fail("external routes must not be rebuilt"))
        async def dsp_state():
            return {"active": False, "enabled_setting": True, "default": current_default}
        plugin._dsp_state = dsp_state
        async with plugin.engine_lock:
            result = await plugin._apply_current_locked(force_restart=True)
        assert result == {"profile": "movie", "voicing": "warm", "verified": False}
        assert calls == ["stop-owned-engine", ("pending-selection", "movie", "warm")]
        assert plugin.settings["enabled"] is True
        assert plugin.settings["global"] == {"profile": "movie", "voicing": "warm"}
        assert events[0] == ("dsp_state", {"active": False, "enabled_setting": True, "default": current_default})
        assert events[1][0] == "original_controls_state"

    asyncio.run(scenario())
