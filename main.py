"""Decky Loader entry point; async facade over py_modules/allydsp."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, Optional

import decky  # type: ignore

sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), "py_modules"))

from allydsp import asus_fetch, convert, deckyfix, diagnostics, dsp_runtime, hardware, original_runtime, paths, settings, setup_flow, updater  # noqa: E402
from allydsp.constants import PROFILES, VOICING_LABELS, VOICINGS  # noqa: E402
from allydsp.jackwatch import JackWatcher  # noqa: E402


class Plugin:
    # ---------------------------------------------------------------- lifecycle
    async def _main(self):
        if not deckyfix.park_reader_at_eof():
            decky.logger.warning("Decky socket workaround not applied; stopping may take 5 s")
        self.loop = asyncio.get_event_loop()
        paths.ensure_dirs()
        self.settings: Dict[str, Any] = settings.load()
        self.engine_lock = asyncio.Lock()
        self.running_app: Optional[str] = None
        self.setup_task: Optional[asyncio.Task] = None
        self.setup_cancel = False
        self.setup_last: Optional[Dict[str, Any]] = None
        self.convert_task: Optional[asyncio.Task] = None
        self.convert_last: Optional[Dict[str, Any]] = None
        self.update_task: Optional[asyncio.Task] = None
        self.lv2_cache: Optional[Dict[str, Any]] = None
        self.jack = JackWatcher(on_change=self._on_jack_change, lifecycle_lock=self.engine_lock)
        self.jack.start(self._should_run)
        self.loop.create_task(self._startup())
        decky.logger.info("Ally DSP backend started (plugin dir %s)", paths.PLUGIN_DIR)

    # _unload and _uninstall do not await: if deckyfix could not stop Decky's socket loop from
    # spinning, the event loop never runs again once the stop begins.
    async def _unload(self):
        for t in (getattr(self.jack, "_task", None), self.setup_task, self.convert_task, self.update_task):
            if t and not t.done():
                t.cancel()
        decky.logger.info("Ally DSP backend unloaded (filter chain keeps running)")

    async def _uninstall(self):
        # Decky also calls this while replacing the plugin during an update, so the unit and
        # runtime data are only removed if the plugin is still gone a minute later.
        try:
            if dsp_runtime.schedule_removal():
                decky.logger.info("uninstall: unit and runtime data go in %ss unless the plugin comes back",
                                  dsp_runtime.REMOVAL_DELAY_S)
        except Exception as e:
            decky.logger.error("uninstall cleanup failed: %s", e)

    async def _startup(self):
        try:
            await asyncio.to_thread(dsp_runtime.cancel_removal)
            updater.remove_legacy_marker()
            if self.settings["setup"].get("done") or dsp_runtime.engine_kind() == "original":
                await self._reconcile()
            if self.settings["update"].get("autoCheck", True):
                await self.check_for_update(False)
        except Exception as e:
            decky.logger.error("startup reconcile failed: %s", e)

    async def _reconcile(self) -> None:
        """Bring unit and active preset back after an update or lost data."""
        res = settings.resolve(self.settings, self.running_app)
        if dsp_runtime.engine_kind() == "original":
            await asyncio.to_thread(dsp_runtime.enable, False)
            if self.settings.get("enabled"):
                await self._apply_current(force_restart=True)
            else:
                await asyncio.to_thread(dsp_runtime.stop)
            return
        have_presets = any(v for p in convert.list_presets().values() for v in p.values())
        if not have_presets or not asus_fetch.current_xml() or not await asyncio.to_thread(convert.venv_ok):
            decky.logger.warning("setup data incomplete, running setup again")
            await self.run_setup(False, False)
            return
        await asyncio.to_thread(dsp_runtime.ensure_unit)
        if self.settings.get("enabled"):
            await asyncio.to_thread(dsp_runtime.enable, True)
            if not convert.preset_available(res["profile"], res["voicing"]):
                return
            active = dsp_runtime.active_meta() or {}
            if not os.path.isfile(dsp_runtime.ACTIVE_CONF) or active.get("profile") != res["profile"] \
                    or active.get("voicing") != res["voicing"]:
                await self._apply_current(force_restart=True)
            elif not await asyncio.to_thread(dsp_runtime.is_active):
                dump = await asyncio.to_thread(hardware.pw_dump)
                if not hardware.headphones_active(hardware.output_route(dump)):
                    await asyncio.to_thread(dsp_runtime.start)

    # ---------------------------------------------------------------- helpers
    def _should_run(self) -> bool:
        return bool(self.settings.get("enabled") and
                    (self.settings.get("engine", {}).get("kind") == "original" or self.settings["setup"].get("done")))

    def _setup_running(self) -> bool:
        return bool(self.setup_task and not self.setup_task.done())

    def _emit_threadsafe(self, event: str, payload: Any) -> None:
        self.loop.call_soon_threadsafe(lambda: self.loop.create_task(decky.emit(event, payload)))

    async def _on_jack_change(self, state: Dict[str, Any]):
        # JackWatcher invokes this while holding engine_lock. Original Game
        # has separate verified speaker/headphone configurations; start() only
        # restarts the worker when the active analog route selects another one.
        if dsp_runtime.engine_kind() == "original" and self._should_run():
            await self._apply_current_locked()
        await decky.emit("dsp_state", await self._dsp_state())

    async def _dsp_state(self) -> Dict[str, Any]:
        st = await asyncio.to_thread(dsp_runtime.status, False)
        st["jack"] = self.jack.state()
        st["enabled_setting"] = bool(self.settings.get("enabled"))
        return st

    def _save(self) -> None:
        # setup_flow writes "setup" from its worker thread; never overwrite it with this copy.
        settings.save_keeping(self.settings, "setup")

    async def _apply_current(self, force_restart: bool = False) -> Optional[Dict[str, Any]]:
        async with self.engine_lock:
            return await self._apply_current_locked(force_restart)

    async def _apply_current_locked(self, force_restart: bool = False) -> Optional[Dict[str, Any]]:
        """Apply the resolved preset; restart the unit only when it changes."""
        res = settings.resolve(self.settings, self.running_app)
        if dsp_runtime.engine_kind() == "original":
            want_running = self._should_run()
            result = await asyncio.to_thread(dsp_runtime.apply_preset, res["profile"], res["voicing"], 0.0, want_running)
            if not want_running and await asyncio.to_thread(dsp_runtime.is_active):
                await asyncio.to_thread(dsp_runtime.stop)
            await decky.emit("dsp_state", await self._dsp_state())
            return result
        if not convert.preset_available(res["profile"], res["voicing"]):
            decky.logger.warning("preset %s/%s not available yet", res["profile"], res["voicing"])
            return None
        pregain = settings.clamp_pregain(self.settings["extras"].get("preGainDb", 0))
        active = dsp_runtime.active_meta() or {}
        unchanged = (active.get("profile") == res["profile"] and active.get("voicing") == res["voicing"]
                     and abs(float(active.get("preGainDb", 0) or 0) - pregain) < 1e-6)
        want_running = self._should_run() and not self.jack.paused
        if unchanged and not force_restart:
            if want_running and not await asyncio.to_thread(dsp_runtime.is_active):
                await asyncio.to_thread(dsp_runtime.start)
            return active
        result = await asyncio.to_thread(dsp_runtime.apply_preset, res["profile"], res["voicing"], pregain, want_running)
        if not want_running and await asyncio.to_thread(dsp_runtime.is_active):
            await asyncio.to_thread(dsp_runtime.stop)
        await decky.emit("dsp_state", await self._dsp_state())
        return result

    # ---------------------------------------------------------------- state
    async def get_state(self) -> Dict[str, Any]:
        dump = await asyncio.to_thread(hardware.pw_dump)
        codec = await asyncio.to_thread(hardware.codec_info)
        if self.lv2_cache is None:
            self.lv2_cache = await asyncio.to_thread(hardware.lv2_check)
        route = hardware.output_route(dump)
        setup = dict(self.settings["setup"])
        setup.update({
            "xmlPresent": bool(asus_fetch.current_xml()),
            "venvOk": await asyncio.to_thread(convert.venv_ok),
            "presets": convert.list_presets(),
            "inProgress": self._setup_running(),
            "last": self.setup_last,
            "converting": bool(self.convert_task and not self.convert_task.done()),
            "convertLast": self.convert_last,
        })
        return {
            "version": decky.DECKY_PLUGIN_VERSION,
            "setup": setup,
            "hardware": {"codec": codec, "dmi": hardware.dmi(), "kernel": hardware.kernel(),
                         "sink": hardware.find_speaker_sink(dump), "route": route,
                         "headphones": hardware.headphones_active(route), "lv2": self.lv2_cache},
            "dsp": await self._dsp_state(),
            "settings": self.settings,
            "engine": await asyncio.to_thread(dsp_runtime.engine_status),
            "runningApp": {"appId": self.running_app, "resolved": settings.resolve(self.settings, self.running_app)},
            "update": self._update_info(),
            "profiles": [{"id": p, "label": l} for p, l in PROFILES],
            "voicings": [{"id": v, "label": VOICING_LABELS[v]} for v in VOICINGS],
        }

    async def set_engine(self, kind: str) -> Dict[str, Any]:
        if kind not in ("converted", "original"):
            raise ValueError("Unknown audio engine")
        if self._setup_running() or (self.convert_task and not self.convert_task.done()):
            raise RuntimeError("Wait for setup or preset conversion to finish")
        async with self.engine_lock:
            if kind == dsp_runtime.engine_kind():
                return {"ok": True, "engine": await asyncio.to_thread(dsp_runtime.engine_status)}
            if kind == "original":
                ready = await asyncio.to_thread(original_runtime.availability)
                if not ready["available"]:
                    raise RuntimeError(ready["error"])
            snapshot = json.loads(json.dumps(self.settings))
            was_active = await asyncio.to_thread(dsp_runtime.is_active)
            try:
                if not await asyncio.to_thread(dsp_runtime.stop):
                    raise RuntimeError("Could not stop the current audio engine")
                self.settings.setdefault("engine", {})["kind"] = kind
                self._save()
                await asyncio.to_thread(dsp_runtime.ensure_unit)
                await asyncio.to_thread(dsp_runtime.enable, self._should_run())
                await self._apply_current_locked(force_restart=True)
            except Exception as error:
                rollback_error = None
                try:
                    await asyncio.to_thread(dsp_runtime.stop)
                    self.settings = snapshot
                    self._save()
                    await asyncio.to_thread(dsp_runtime.ensure_unit)
                    await asyncio.to_thread(dsp_runtime.enable, self._should_run())
                    if was_active:
                        await self._apply_current_locked(force_restart=True)
                except Exception as rollback:
                    rollback_error = str(rollback)
                suffix = "; recovery failed: " + rollback_error if rollback_error else "; previous engine restored"
                raise RuntimeError(str(error) + suffix) from error
        return {"ok": True, "engine": await asyncio.to_thread(dsp_runtime.engine_status)}

    # ---------------------------------------------------------------- setup
    async def run_setup(self, force: bool = False, allow_unsupported: bool = False) -> Dict[str, Any]:
        if dsp_runtime.engine_kind() == "original":
            raise RuntimeError("Select the converted engine before running its setup")
        if self.setup_task and not self.setup_task.done():
            return {"started": False, "reason": "already running"}
        self.setup_cancel = False
        self.setup_last = None

        def progress(ev: Dict[str, Any]) -> None:
            self.setup_last = ev
            self._emit_threadsafe("setup_progress", ev)

        async def runner():
            try:
                await asyncio.to_thread(setup_flow.run_setup, progress, bool(force), True, bool(allow_unsupported),
                                        lambda: self.setup_cancel, True)
                self.settings = settings.load()
                self.lv2_cache = None
                ev = {"step": "finished", "status": "done", "message": "Setup complete", "percent": 100,
                      "index": len(setup_flow.STEPS), "total": len(setup_flow.STEPS)}
            except setup_flow.Cancelled:
                ev = {"step": "finished", "status": "cancelled", "message": "Setup cancelled", "percent": 0,
                      "index": len(setup_flow.STEPS), "total": len(setup_flow.STEPS)}
            except Exception as e:
                decky.logger.error("setup failed: %s", e)
                ev = {"step": (self.setup_last or {}).get("step", "hardware"), "status": "error", "message": str(e),
                      "percent": (self.setup_last or {}).get("percent", 0), "index": (self.setup_last or {}).get("index", 0),
                      "total": len(setup_flow.STEPS)}
            self.setup_last = ev
            await decky.emit("setup_progress", ev)
            if ev["status"] == "done":
                await self._catch_up_after_setup()
            await decky.emit("dsp_state", await self._dsp_state())

        self.setup_task = self.loop.create_task(runner())
        return {"started": True}

    async def _catch_up_after_setup(self) -> None:
        """Setup converts and activates what it read at its start; apply what changed meanwhile."""
        try:
            if settings.extras_signature(self.settings["extras"]) != self.settings["setup"].get("extrasSignature"):
                await self._start_reconvert()
            else:
                await self._apply_current()
        except Exception as e:
            decky.logger.error("applying settings after setup failed: %s", e)

    async def cancel_setup(self) -> Dict[str, Any]:
        self.setup_cancel = True
        return {"cancelling": bool(self.setup_task and not self.setup_task.done())}

    async def get_setup_progress(self) -> Optional[Dict[str, Any]]:
        return self.setup_last

    async def import_xml(self, path: str) -> Dict[str, Any]:
        codec = await asyncio.to_thread(hardware.codec_info)
        if not codec:
            raise RuntimeError("no Realtek codec found")
        prov = await asyncio.to_thread(asus_fetch.import_xml, path, codec)
        return {"ok": True, "provenance": prov}

    # ---------------------------------------------------------------- presets
    async def set_global(self, profile: str, voicing: str) -> Dict[str, Any]:
        if not (settings.valid_profile(profile) and settings.valid_voicing(voicing)):
            raise ValueError("invalid profile or voicing")
        self.settings["global"] = {"profile": profile, "voicing": voicing}
        self._save()
        await self._apply_current()
        return {"ok": True, "resolved": settings.resolve(self.settings, self.running_app)}

    async def set_per_app(self, app_id: str, entry: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        app_id = str(app_id)
        per = self.settings.setdefault("perApp", {})
        if entry is None:
            per.pop(app_id, None)
        else:
            if not (settings.valid_profile(entry.get("profile")) and settings.valid_voicing(entry.get("voicing"))):
                raise ValueError("invalid profile or voicing")
            per[app_id] = {"profile": entry["profile"], "voicing": entry["voicing"],
                           "enabled": bool(entry.get("enabled", True)), "name": str(entry.get("name", ""))[:80]}
        self._save()
        if self.running_app == app_id:
            await self._apply_current()
        return {"ok": True, "perApp": per}

    async def on_running_app_changed(self, app_id: Optional[str]) -> Dict[str, Any]:
        new = str(app_id) if app_id not in (None, "", "0", 0) else None
        if new != self.running_app:
            self.running_app = new
            decky.logger.info("running app changed: %s", new)
            if self._should_run():
                await self._apply_current()
        return {"appId": self.running_app, "resolved": settings.resolve(self.settings, self.running_app)}

    async def set_enabled(self, enabled: bool) -> Dict[str, Any]:
        async with self.engine_lock:
            self.settings["enabled"] = bool(enabled)
            self._save()
            if enabled:
                await asyncio.to_thread(dsp_runtime.ensure_unit)
                await asyncio.to_thread(dsp_runtime.enable, True)
                await self._apply_current_locked(force_restart=True)
            else:
                await asyncio.to_thread(dsp_runtime.stop)
                await asyncio.to_thread(dsp_runtime.enable, False)
        state = await self._dsp_state()
        await decky.emit("dsp_state", state)
        return state

    async def set_extras(self, extras: Dict[str, Any]) -> Dict[str, Any]:
        if dsp_runtime.engine_kind() == "original":
            raise RuntimeError("These controls apply to the converted engine; its saved settings are preserved")
        old = dict(self.settings["extras"])
        new = dict(old)
        for k in ("autogain", "dialog", "regulator", "virtualBass"):
            if k in extras:
                new[k] = bool(extras[k])
        if "preGainDb" in extras:
            new["preGainDb"] = settings.clamp_pregain(extras["preGainDb"])
        self.settings["extras"] = new
        self._save()
        if self._setup_running():
            pass  # _catch_up_after_setup applies the change
        elif settings.extras_signature(new) != settings.extras_signature(old):
            await self._start_reconvert()
        elif abs(float(new.get("preGainDb", 0)) - float(old.get("preGainDb", 0))) > 1e-6:
            await self._apply_current()
        return {"ok": True, "extras": new, "reconverting": bool(self.convert_task and not self.convert_task.done())}

    async def _start_reconvert(self) -> None:
        if self.convert_task and not self.convert_task.done():
            return
        xml = asus_fetch.current_xml()
        sink = self.settings["setup"].get("targetSink")
        if not xml or not sink:
            return

        def progress(pct: float, msg: str) -> None:
            self.convert_last = {"percent": pct, "message": msg, "status": "running"}
            self._emit_threadsafe("convert_progress", self.convert_last)

        async def runner():
            try:
                while True:  # extras may change again while a conversion runs
                    extras = dict(self.settings["extras"])
                    await asyncio.to_thread(convert.convert_all, xml, sink, extras, progress)
                    sig = settings.extras_signature(extras)
                    self.settings["setup"] = settings.update_section("setup", {"extrasSignature": sig})["setup"]
                    if settings.extras_signature(self.settings["extras"]) == sig:
                        break
                self.convert_last = {"percent": 100, "message": "Presets regenerated", "status": "done"}
                await self._apply_current(force_restart=True)
            except Exception as e:
                decky.logger.error("reconvert failed: %s", e)
                self.convert_last = {"percent": 0, "message": str(e), "status": "error"}
            await decky.emit("convert_progress", self.convert_last)

        self.convert_task = self.loop.create_task(runner())

    # ---------------------------------------------------------------- updates
    async def check_for_update(self, force: bool = False) -> Dict[str, Any]:
        state = dict(self.settings["update"])  # the worker thread fills a copy
        res = await asyncio.to_thread(updater.check, state, decky.DECKY_PLUGIN_VERSION, bool(force))
        self.settings["update"] = state
        self._save()
        await decky.emit("update_state", res)
        return res

    def _update_info(self) -> Dict[str, Any]:
        """Cached update state; a due check runs in the background and reports through update_state."""
        state = self.settings["update"]
        if state.get("autoCheck", True) and updater.check_due(state) \
                and not (self.update_task and not self.update_task.done()):
            self.update_task = self.loop.create_task(self.check_for_update(False))
        return updater.check(state, decky.DECKY_PLUGIN_VERSION, fetch=False)

    async def prepare_update(self) -> Dict[str, Any]:
        latest = self.settings["update"].get("latest")
        if not latest:
            latest = await asyncio.to_thread(updater.fetch_latest)
            self.settings["update"]["latest"] = latest
            self._save()
        return await asyncio.to_thread(updater.verify_release, latest)

    # ---------------------------------------------------------------- diagnostics
    async def get_diagnostics(self) -> Dict[str, Any]:
        d = await asyncio.to_thread(diagnostics.collect)
        text = diagnostics.render_text(d)
        try:
            os.makedirs(paths.LOG_DIR, exist_ok=True)
            with open(os.path.join(paths.LOG_DIR, "diagnostics.txt"), "w", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass
        return {"text": text}
