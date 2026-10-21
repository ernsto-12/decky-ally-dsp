"""Original-DLL runtime lifecycle and reversible routing, in the user's session.

The service name is shared with the converted engine. It is never a second
audio service running alongside it. Private DLLs and tuning remain in data/.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from typing import Any, Dict

from . import converted_runtime, hardware, paths, settings
from .log import logger
from .util import atomic_write_bytes, atomic_write_text, read_json, run, user_env, write_json

ORIGINAL_DIR = os.path.join(paths.RUNTIME_DIR, "original")
ENGINE_FILE = os.path.join(ORIGINAL_DIR, "engine.json")
RUNTIME_FILE = os.path.join(ORIGINAL_DIR, "runtime.json")
STATUS_FILE = os.path.join(ORIGINAL_DIR, "status.json")
ROUTING_FILE = os.path.join(ORIGINAL_DIR, "routing.json")
META_FILE = os.path.join(ORIGINAL_DIR, "active.json")
CAPTURE_SINK = "ally_dsp_original_input"
NAMESPACE = "ally_dsp_original"
POSITIONS = ["FL", "FR", "FC", "LFE", "RL", "RR"]
_hash_cache = {}
HEALTH_TIMEOUT_SECONDS = 3
HEARTBEAT_MAX_AGE_SECONDS = 8


def _json_command(args):
    result = run(args, timeout=15)
    if not result.ok:
        raise RuntimeError(result.err.strip() or "Audio session command failed")
    return json.loads(result.out)


def _sinks():
    return _json_command(["pactl", "-f", "json", "list", "sinks"])


def _inputs():
    return _json_command(["pactl", "-f", "json", "list", "sink-inputs"])


def _checked(args):
    result = run(args, timeout=15)
    if not result.ok:
        raise RuntimeError(result.err.strip() or "Audio routing command failed")
    return result.out.strip()


def _hash_file(path):
    stat = os.stat(path)
    identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    cached = _hash_cache.get(path)
    if cached and cached[0] == identity:
        return cached[1]
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    result = h.hexdigest()
    _hash_cache[path] = (identity, result)
    return result


def _configuration():
    config = read_json(ENGINE_FILE)
    if not isinstance(config, dict) or config.get("schema") != "ally-dsp-original-engine-v1":
        raise RuntimeError("Import the locally owned Dolby engine assets first")
    if config.get("sample_rate") != 48000 or config.get("input_channels") != 6:
        raise RuntimeError("This runtime requires a verified 48 kHz / six-channel engine")
    files = config.get("files", [])
    if not files:
        raise RuntimeError("The engine asset manifest is empty")
    for item in files:
        path = item.get("path", "")
        if not os.path.isabs(path) or not os.path.isfile(path) or os.path.islink(path):
            raise RuntimeError("Missing regular engine asset")
        if _hash_file(path) != item.get("sha256"):
            raise RuntimeError("An imported engine asset changed: " + os.path.basename(path))
    profiles = config.get("profiles")
    if profiles:
        route = hardware.output_route(hardware.pw_dump())
        selected = "headphones" if hardware.headphones_active(route) else "speakers"
        profile = profiles.get(selected)
        if not isinstance(profile, dict):
            raise RuntimeError("The imported engine pack has no configuration for this output")
        config = dict(config, **profile, engine_profile=selected)
    else:
        # Early imported packs carried one explicit headphone profile.
        config = dict(config, engine_profile="headphones")
    worker = config.get("worker", {})
    command = worker.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and "\0" not in x for x in command):
        raise RuntimeError("Missing original-engine worker command")
    if not os.path.isfile(os.path.join(paths.PLUGIN_DIR, "py_modules", "allydsp", "original", "pipewire_bridge.py")):
        raise RuntimeError("The original-engine bridge is not installed")
    return config


def availability() -> Dict[str, Any]:
    try:
        config = _configuration()
        return {"available": True, "error": None,
                "engineLabel": config.get("label", "Original Dolby engine"),
                "scope": config.get("scope", "Experimental original-DLL rendering")}
    except Exception as error:
        return {"available": False, "error": str(error), "engineLabel": "Original Dolby engine", "scope": None}


def _unit_arg(value):
    if any(c in value for c in ["\0", "\r", "\n"]):
        raise ValueError("Invalid service argument")
    # systemd performs its own % specifier expansion inside quoted arguments.
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def unit_text() -> str:
    script = os.path.join(paths.PLUGIN_DIR, "py_modules", "allydsp", "original", "pipewire_bridge.py")
    python = _unit_arg(paths.SYSTEM_PYTHON)
    recovery = _recovery_script()
    environment = ["Environment=" + _unit_arg(key + "=" + value) for key, value in _path_environment().items()]
    return "\n".join([
        "[Unit]", "Description=Ally DSP original Dolby audio engine", "After=pipewire.service pipewire-pulse.service",
        "BindsTo=pipewire.service", "ConditionPathExists=" + ENGINE_FILE.replace("%", "%%"), "", "[Service]",
        "Type=simple", *environment,
        "ExecStart=" + python + " " + _unit_arg(script) + " --config " + _unit_arg(RUNTIME_FILE),
        "ExecStop=" + python + " " + _unit_arg(recovery),
        "ExecStopPost=" + python + " " + _unit_arg(recovery),
        "Restart=no", "KillMode=control-group", "TimeoutStopSec=12", "", "[Install]", "WantedBy=default.target", ""])


def _path_environment():
    return {"ALLYDSP_PLUGIN_DIR": paths.PLUGIN_DIR, "DECKY_USER_HOME": paths.HOME,
            "DECKY_USER": paths.USER, "DECKY_PLUGIN_RUNTIME_DIR": paths.RUNTIME_DIR,
            "DECKY_PLUGIN_SETTINGS_DIR": paths.SETTINGS_DIR, "DECKY_PLUGIN_LOG_DIR": paths.LOG_DIR}


def _recovery_script():
    return os.path.join(ORIGINAL_DIR, "recovery", "allydsp", "original_recover.py")


def _ensure_recovery():
    # Decky removes a plugin's code before its deferred uninstall cleanup runs.
    # Keep the small, vendor-free recovery modules in private runtime data so
    # ExecStopPost can restore application routes even after removal/update.
    source = os.path.join(paths.PLUGIN_DIR, "py_modules", "allydsp")
    destination = os.path.dirname(_recovery_script())
    if not os.path.isdir(source):
        if not os.path.isfile(_recovery_script()):
            raise RuntimeError("The audio recovery helper is unavailable")
        return
    os.makedirs(destination, exist_ok=True)
    for entry in os.scandir(source):
        if entry.name.endswith(".py") and entry.is_file(follow_symlinks=False):
            with open(entry.path, "rb") as stream:
                content = stream.read()
            output = os.path.join(destination, entry.name)
            try:
                with open(output, "rb") as stream:
                    if stream.read() == content:
                        continue
            except OSError:
                pass
            atomic_write_bytes(output, content, mode=0o600)


def _routing_action(action):
    # Decky's bundled interpreter need not include fcntl. The system Python
    # performs routing mutations under one inter-process flock, including
    # ExecStopPost after a crash. Never hold this lock while stopping systemd.
    _ensure_recovery()
    script = _recovery_script()
    result = run([paths.SYSTEM_PYTHON, script, "--action", action], timeout=60,
                 env=user_env(_path_environment()))
    if not result.ok:
        raise RuntimeError(result.err.strip() or "Original audio routing recovery failed")


def unit_installed() -> bool:
    try:
        with open(paths.UNIT_PATH, encoding="utf-8") as stream:
            return stream.read() == unit_text()
    except OSError:
        return False


def install_unit() -> None:
    _ensure_recovery()
    os.makedirs(os.path.dirname(paths.UNIT_PATH), exist_ok=True)
    atomic_write_text(paths.UNIT_PATH, unit_text())
    result = converted_runtime.systemctl("daemon-reload")
    if not result.ok:
        raise RuntimeError("Could not reload the original audio service")


def ensure_unit() -> None:
    if not unit_installed():
        install_unit()


def _stream_identity(stream):
    props = stream.get("properties", {})
    return {key: props.get(key) for key in ["application.process.id", "application.id", "media.name", "object.serial"]}


def restore_routing() -> None:
    # Do not launch a subprocess for the usual converted-engine-only case.
    journal = read_json(ROUTING_FILE)
    if isinstance(journal, dict) and not journal.get("restored"):
        _routing_action("restore")


def _owns_sink(sink, journal):
    return bool(sink and journal.get("token") and
                (journal.get("module_id") is None or str(sink.get("owner_module")) == str(journal.get("module_id"))) and
                sink.get("properties", {}).get("allydsp.session") == journal["token"])


_STEREO_CHANNELS = ("front-left", "front-right")


def _sink_volume(sink, channels):
    values = []
    for channel in channels:
        value = sink.get("volume", {}).get(channel, {}).get("value")
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2147483647:
            raise RuntimeError("The audio output has no valid absolute channel volume")
        values.append(value)
    return values


def _sink_mute(sink):
    value = sink.get("mute")
    if not isinstance(value, bool):
        raise RuntimeError("The audio output has no valid mute state")
    return value


def _physical_identity(sink):
    return {"name": sink.get("name"), "index": sink.get("index"),
            "serial": sink.get("properties", {}).get("object.serial")}


def _same_physical(sink, handoff):
    expected = handoff["physical_identity"]
    actual = _physical_identity(sink) if sink else {}
    return all(actual.get(key) is not None and str(actual[key]) == str(expected.get(key))
               for key in ("name", "index", "serial"))


def _volume_handoff(journal):
    handoff = journal.get("volume_handoff")
    if handoff is None:
        return None  # Journals from before master-volume handoff own no levels.
    if handoff.get("schema") != "ally-dsp-original-volume-handoff-v1":
        raise RuntimeError("Unknown original audio volume-recovery journal")
    return handoff


def _surround_volume(volume):
    left, right = volume
    center = round(((left ** 3 + right ** 3) / 2) ** (1 / 3))
    return [left, right, center, center, left, right]


def _sync_virtual_master(journal, phase):
    handoff = _volume_handoff(journal)
    if handoff is None or journal.get("routed") or any(handoff[key].get("intent") for key in ("volume", "mute")):
        return
    sinks = _sinks()
    owned = next((sink for sink in sinks if sink.get("name") == CAPTURE_SINK), None)
    physical = next((sink for sink in sinks if sink.get("name") == journal["physical_sink"]), None)
    if not _owns_sink(owned, journal) or not _same_physical(physical, handoff):
        raise RuntimeError("Audio output ownership changed before copying the master volume")
    latest = {"volume": _sink_volume(physical, _STEREO_CHANNELS), "mute": _sink_mute(physical)}
    target = {"volume": _surround_volume(latest["volume"]), "mute": latest["mute"]}
    if phase == "ready":
        current = {"volume": _sink_volume(owned, (*_STEREO_CHANNELS, "front-center", "lfe", "rear-left", "rear-right")),
                   "mute": _sink_mute(owned)}
        for control in ("volume", "mute"):
            virtual_changed = current[control] != handoff["virtual_initial"][control]
            physical_changed = latest[control] != handoff["original"][control]
            if virtual_changed and physical_changed and current[control] != target[control]:
                raise RuntimeError("Both output masters changed during startup; volume handoff was cancelled")
            if virtual_changed and not physical_changed:
                target[control] = current[control]
    # READY may take seconds. Preserve the most recent physical state and any
    # independent virtual adjustment before moving a game or taking its master.
    handoff.update(original=latest, virtual_initial=target, virtual_sync_phase=phase)
    write_json(ROUTING_FILE, journal)
    for control in ("volume", "mute"):
        current_owned = next((sink for sink in _sinks() if sink.get("name") == CAPTURE_SINK), None)
        if not _owns_sink(current_owned, journal):
            raise RuntimeError("The virtual output changed during initial master-volume copy")
        handoff["virtual_" + control + "_intent"] = True
        write_json(ROUTING_FILE, journal)
        args = (["pactl", "set-sink-volume", CAPTURE_SINK, *map(str, target[control])] if control == "volume"
                else ["pactl", "set-sink-mute", CAPTURE_SINK, "1" if target[control] else "0"])
        _checked(args)
    # Keep actual Pulse integer rounding as the baseline for conflict detection.
    current_owned = next((sink for sink in _sinks() if sink.get("name") == CAPTURE_SINK), None)
    if not _owns_sink(current_owned, journal):
        raise RuntimeError("The virtual output changed after initial master-volume copy")
    handoff["virtual_initial"] = {
        "volume": _sink_volume(current_owned, (*_STEREO_CHANNELS, "front-center", "lfe", "rear-left", "rear-right")),
        "mute": _sink_mute(current_owned)}
    write_json(ROUTING_FILE, journal)


def _handoff_physical_master(journal):
    handoff = _volume_handoff(journal)
    if handoff is None:
        return
    for control, desired in (("volume", [65536, 65536]), ("mute", False)):
        step = handoff[control]
        if step.get("applied"):
            continue
        if step.get("intent"):
            raise RuntimeError("An unfinished master-volume handoff needs recovery")
        sinks = _sinks()
        owned = next((sink for sink in sinks if sink.get("name") == CAPTURE_SINK), None)
        physical = next((sink for sink in sinks if sink.get("name") == journal["physical_sink"]), None)
        if not _owns_sink(owned, journal) or not _same_physical(physical, handoff):
            raise RuntimeError("Audio output ownership changed before master-volume handoff")
        # Persist ownership and the intended value before either physical change.
        # Recovery also handles a crash after pactl but before the applied write.
        step.update(intent=True, expected=desired)
        write_json(ROUTING_FILE, journal)
        args = (["pactl", "set-sink-volume", physical["name"], *map(str, desired)] if control == "volume"
                else ["pactl", "set-sink-mute", physical["name"], "0"])
        _checked(args)
        current = next((sink for sink in _sinks() if sink.get("name") == physical["name"]), None)
        if not _same_physical(current, handoff):
            raise RuntimeError("The physical output changed during master-volume handoff")
        actual = _sink_volume(current, _STEREO_CHANNELS) if control == "volume" else _sink_mute(current)
        # Pulse's float conversion may round unity by a few raw integer units.
        if (control == "volume" and any(abs(value - 65536) > 4 for value in actual)) or (control == "mute" and actual):
            raise RuntimeError("The physical output did not accept the master-volume handoff")
        step.update(expected=actual, applied=True)
        write_json(ROUTING_FILE, journal)


def _restore_physical_master(journal, owned):
    handoff = _volume_handoff(journal)
    if handoff is None or not any(handoff[key].get("intent") for key in ("volume", "mute")):
        return
    if owned is not None and not _owns_sink(owned, journal):
        raise RuntimeError("The original sink was replaced before volume recovery")
    if "restore" not in handoff:
        # Freeze both targets before the first mutation so retries never derive
        # a new target from an already-restored/replaced/disappeared sink.
        target = ({"volume": _sink_volume(owned, _STEREO_CHANNELS), "mute": _sink_mute(owned)}
                  if owned else handoff["original"])
        handoff["restore"] = {key: {"target": target[key], "intent": False, "done": False}
                              for key in ("volume", "mute")}
        handoff["restore_source"] = "owned-virtual" if owned else "original-physical-fallback"
        write_json(ROUTING_FILE, journal)
    for control in ("volume", "mute"):
        step = handoff["restore"][control]
        if step.get("done") or not handoff[control].get("intent"):
            continue
        sinks = _sinks()
        current_owned = next((sink for sink in sinks if sink.get("name") == CAPTURE_SINK), None)
        if current_owned is not None and not _owns_sink(current_owned, journal):
            raise RuntimeError("The original sink was replaced during volume recovery")
        physical = next((sink for sink in sinks if sink.get("name") == journal["physical_sink"]), None)
        if physical is None:
            raise RuntimeError("The physical output is unavailable; volume recovery is pending")
        if not _same_physical(physical, handoff):
            step.update(done=True, preserved_external=True, reason="physical-identity-changed")
        else:
            actual = _sink_volume(physical, _STEREO_CHANNELS) if control == "volume" else _sink_mute(physical)
            if actual == step["target"]:
                step.update(done=True, already_at_target=True)
            elif (actual != handoff[control]["expected"]
                  and not (control == "volume" and not handoff[control].get("applied")
                           and handoff[control]["expected"] == [65536, 65536]
                           and all(abs(value - 65536) <= 4 for value in actual))):
                step.update(done=True, preserved_external=True)
            else:
                step["intent"] = True
                write_json(ROUTING_FILE, journal)
                args = (["pactl", "set-sink-volume", physical["name"], *map(str, step["target"])] if control == "volume"
                        else ["pactl", "set-sink-mute", physical["name"], "1" if step["target"] else "0"])
                _checked(args)
                step["done"] = True
        write_json(ROUTING_FILE, journal)


def _mirror_sinks(journal):
    mirror = journal.get("master_mirror")
    if not isinstance(mirror, dict) or mirror.get("schema") != "ally-dsp-original-physical-master-v1":
        raise RuntimeError("Unknown original physical-master journal")
    sinks = _sinks()
    owned = next((sink for sink in sinks if sink.get("name") == CAPTURE_SINK), None)
    physical = next((sink for sink in sinks if sink.get("name") == journal["physical_sink"]), None)
    if not _owns_sink(owned, journal) or not _same_physical(physical, mirror):
        raise RuntimeError("Audio output ownership changed during master synchronization")
    return physical, owned


def _mirror_levels(sink, virtual=False):
    channels = (*_STEREO_CHANNELS, "front-center", "lfe", "rear-left", "rear-right") if virtual else _STEREO_CHANNELS
    return {"volume": _sink_volume(sink, channels), "mute": _sink_mute(sink)}


def _write_mirror_control(journal, target, control, value):
    physical, owned = _mirror_sinks(journal)
    sink = physical if target == "physical" else owned
    journal["master_mirror"]["intent"] = {"control": control, "target": target, "value": value}
    write_json(ROUTING_FILE, journal)
    args = (["pactl", "set-sink-volume", sink["name"], *map(str, value)] if control == "volume"
            else ["pactl", "set-sink-mute", sink["name"], "1" if value else "0"])
    _checked(args)
    current_physical, current_owned = _mirror_sinks(journal)
    observed = _mirror_levels(current_physical if target == "physical" else current_owned, virtual=target == "virtual")
    if target == "virtual":
        matches = (all(abs(actual - expected) <= 4 for actual, expected in zip(observed[control], value))
                   if control == "volume" else observed[control] == value)
        if not matches:
            # Do not commit an intervening UI change as though it were our
            # paired write; the next sync must still observe that new input.
            raise RuntimeError("The virtual master changed while synchronization was being applied")
    return observed


def _mirror_master(journal, physical_only=False):
    mirror = journal["master_mirror"]
    for control in ("volume", "mute"):
        physical, owned = _mirror_sinks(journal)
        physical_levels, virtual_levels = _mirror_levels(physical), _mirror_levels(owned, virtual=True)
        p_value, v_value = physical_levels[control], virtual_levels[control]
        physical_changed = p_value != mirror["last_physical"][control]
        virtual_changed = v_value != mirror["last_virtual"][control]
        pending = mirror.get("intent")
        if (not physical_only and not physical_changed and not virtual_changed
                and (pending is None or pending.get("control") != control)):
            continue  # Readback rounding is a stable baseline, not a new input.
        # Hardware volume keys may address the physical sink even while the
        # virtual sink is the default. Physical wins a simultaneous conflict.
        from_virtual = not physical_only and virtual_changed and not physical_changed
        source = "virtual" if from_virtual else "physical"
        if from_virtual:
            desired = v_value[:2] if control == "volume" else v_value
            if desired != p_value:
                p_value = _write_mirror_control(journal, "physical", control, desired)[control]
        desired_virtual = _surround_volume(p_value) if control == "volume" else p_value
        if v_value != desired_virtual:
            v_value = _write_mirror_control(journal, "virtual", control, desired_virtual)[control]
        pending = mirror.get("intent")
        if (p_value == mirror["last_physical"][control] and v_value == mirror["last_virtual"][control]
                and (pending is None or pending.get("control") != control)):
            continue  # Stable polling does not rewrite the recovery journal.
        # Commit only the paired values used above. A physical change during a
        # later control's update remains different and is seen on the next sync.
        mirror["last_physical"][control] = p_value
        mirror["last_virtual"][control] = v_value
        mirror.update(intent=None, last_direction=source, last_sync_unix_seconds=time.time())
        write_json(ROUTING_FILE, journal)


def sync_master():
    """Synchronize controls through the system-Python routing lock."""
    journal = read_json(ROUTING_FILE)
    if (not isinstance(journal, dict) or journal.get("schema") != "ally-dsp-original-routing-v2"
            or not journal.get("routed") or journal.get("restored") or not verify(0)):
        return False
    _routing_action("sync")
    return True


def _sync_master_locked():
    journal = read_json(ROUTING_FILE)
    if (not isinstance(journal, dict) or journal.get("schema") != "ally-dsp-original-routing-v2"
            or not journal.get("routed") or journal.get("restored")):
        return False
    _mirror_master(journal)
    return True


def _restore_routing_locked() -> None:
    journal = read_json(ROUTING_FILE)
    if (not isinstance(journal, dict) or journal.get("schema") not in
            ("ally-dsp-original-routing-v1", "ally-dsp-original-routing-v2") or journal.get("restored")):
        return
    sinks = _sinks()
    names = {s["name"] for s in sinks}
    owned = next((s for s in sinks if s.get("name") == CAPTURE_SINK), None)
    if owned and not _owns_sink(owned, journal):
        raise RuntimeError("The original sink was replaced; automatic recovery will not modify its routes")
    current_default = _checked(["pactl", "get-default-sink"])
    destination = current_default if current_default != CAPTURE_SINK else journal.get("previous_default")
    if destination not in names or destination == CAPTURE_SINK:
        destination = journal.get("physical_sink")
    if destination not in names or destination == CAPTURE_SINK:
        raise RuntimeError("The previous output is unavailable; routing recovery is pending")
    # Restore the audible master before any application can reach the physical
    # output directly again. Volume and mute each respect external changes.
    if journal["schema"] == "ally-dsp-original-routing-v2":
        if owned and journal.get("routed"):
            try:
                _mirror_master(journal)
            except Exception as error:
                # A missing output must not strand game routes. v2 never owns
                # unity or restores an old level: retain the current master.
                journal["master_sync_recovery_error"] = str(error)
                write_json(ROUTING_FILE, journal)
    else:
        _restore_physical_master(journal, owned)
    if current_default == CAPTURE_SINK:
        _checked(["pactl", "set-default-sink", destination])
    original_routes = {str(s["index"]): s for s in journal.get("moved", [])}
    if owned:
        for stream in _inputs():
            if stream.get("sink") != owned["index"]:
                continue
            previous = original_routes.get(str(stream["index"]))
            target = destination
            if current_default == CAPTURE_SINK and previous and previous.get("identity") == _stream_identity(stream) and previous.get("sink_name") in names:
                target = previous["sink_name"]
            result = run(["pactl", "move-sink-input", str(stream["index"]), target], timeout=15)
            if not result.ok and any(s.get("index") == stream["index"] for s in _inputs()):
                raise RuntimeError(result.err.strip() or "Could not restore an active audio stream")
        _checked(["pactl", "unload-module", str(owned["owner_module"])])
    journal["restored"] = True
    write_json(ROUTING_FILE, journal)
    state = read_json(STATUS_FILE)
    if isinstance(state, dict) and state.get("ready"):
        state.update(ready=False, state="stopped", ended_by_recovery=True)
        write_json(STATUS_FILE, state)


def _route_after_ready_locked():
    journal = read_json(ROUTING_FILE) or {}
    sinks = _sinks()
    sink = next((s for s in sinks if s.get("name") == CAPTURE_SINK), None)
    if not _owns_sink(sink, journal):
        raise RuntimeError("The original audio sink ownership changed")
    physical = next((s for s in sinks if s.get("name") == journal["physical_sink"]), None)
    if not physical:
        raise RuntimeError("The physical output disappeared during startup")
    if not verify(0):
        raise RuntimeError("The original worker exited before routing was applied")
    apply_output_gain()
    if journal.get("schema") == "ally-dsp-original-routing-v2":
        if not journal.get("routed"):
            _mirror_master(journal, physical_only=True)
    else:
        _sync_virtual_master(journal, "ready")
    for stream in _inputs():
        if stream.get("sink") != physical["index"]:
            continue
        properties = stream.get("properties", {})
        if str(properties.get("node.name", "")).startswith(NAMESPACE):
            continue
        record = {"index": stream["index"], "identity": _stream_identity(stream), "sink_name": physical["name"]}
        journal["moved"].append(record)
        write_json(ROUTING_FILE, journal)
        result = run(["pactl", "move-sink-input", str(stream["index"]), CAPTURE_SINK], timeout=15)
        if not result.ok and any(s.get("index") == stream["index"] for s in _inputs()):
            raise RuntimeError(result.err.strip() or "Could not route an active audio stream")
    _checked(["pactl", "set-default-sink", CAPTURE_SINK])
    if journal.get("schema") != "ally-dsp-original-routing-v2":
        _handoff_physical_master(journal)
    journal["routed"] = True
    write_json(ROUTING_FILE, journal)


def _prepare_route_locked():
    _restore_routing_locked()
    sinks = _sinks()
    if any(s.get("name") == CAPTURE_SINK for s in sinks):
        raise RuntimeError("An unowned original-engine sink already exists")
    target = hardware.find_speaker_sink(hardware.pw_dump())
    if not target:
        raise RuntimeError("No supported physical analog output is available")
    physical = next((sink for sink in sinks if sink.get("name") == target["name"]), None)
    if physical is None or _physical_identity(physical)["serial"] is None:
        raise RuntimeError("The physical output has no stable session identity")
    volume, mute = _sink_volume(physical, _STEREO_CHANNELS), _sink_mute(physical)
    virtual_volume = _surround_volume(volume)
    previous = _checked(["pactl", "get-default-sink"])
    if previous == CAPTURE_SINK:
        raise RuntimeError("A previous original-engine route needs recovery")
    os.makedirs(ORIGINAL_DIR, exist_ok=True)
    journal = {"schema": "ally-dsp-original-routing-v2", "previous_default": previous,
               "physical_sink": target["name"], "module_id": None, "moved": [], "restored": False,
               "token": uuid.uuid4().hex,
               "master_mirror": {"schema": "ally-dsp-original-physical-master-v1",
                   "physical_identity": _physical_identity(physical),
                   "last_physical": {"volume": volume, "mute": mute},
                   "last_virtual": {"volume": virtual_volume, "mute": mute}, "intent": None}}
    write_json(ROUTING_FILE, journal)
    module = _checked(["pactl", "load-module", "module-null-sink", "sink_name=" + CAPTURE_SINK,
                           "rate=48000", "channels=6", "channel_map=front-left,front-right,front-center,lfe,rear-left,rear-right",
                           'sink_properties="device.description=AllyDSPOriginal node.virtual=true priority.session=0 channelmix.upmix=false monitor.channel-volumes=false allydsp.session=' + journal["token"] + '"'])
    if not module.isdigit():
        raise RuntimeError("PipeWire returned an invalid sink module identifier")
    journal["module_id"] = int(module)
    write_json(ROUTING_FILE, journal)
    _mirror_master(journal, physical_only=True)


def start() -> bool:
    config = _configuration()
    active_runtime = read_json(RUNTIME_FILE, {}) or {}
    if converted_runtime.is_active() and verify(0) and active_runtime.get("engine_profile") == config["engine_profile"]:
        return True
    # is-active is false during deactivation, while a previous ExecStopPost may
    # still be pending. Finish that stop before writing a new routing journal;
    # otherwise old cleanup could unload the next session's new capture sink.
    ensure_unit()
    if not converted_runtime.stop():
        raise RuntimeError("Could not stop the previous audio service")
    restore_routing()
    try:
        _routing_action("prepare")
        journal = read_json(ROUTING_FILE)
        runtime = {"schema": "original-dll-pipewire-bridge-v1", "namespace": NAMESPACE,
                   "engine_profile": config["engine_profile"],
                   "remote": "pipewire-0", "capture_sink": CAPTURE_SINK, "playback_sink": journal["physical_sink"],
                   "input_positions": POSITIONS, "worker": config["worker"], "status_path": STATUS_FILE,
                   "audit_directory": os.path.join(ORIGINAL_DIR, "audit"), "queue_blocks": 8, "retain_pcm": False}
        write_json(RUNTIME_FILE, runtime)
        ensure_unit()
        if not converted_runtime.start() or not verify(45):
            raise RuntimeError("The original worker did not become ready; the previous output was retained")
        _routing_action("route")
        return True
    except BaseException:
        converted_runtime.stop()
        restore_routing()
        raise


def verify(timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        state = read_json(STATUS_FILE, {}) or {}
        if state.get("ready") and state.get("state") in ("ready", "running") and converted_runtime.is_active():
            main_pid = converted_runtime.systemctl("show", "--property=MainPID", "--value", paths.UNIT_NAME)
            if main_pid.ok and main_pid.out.strip() == str(state.get("daemon_pid")):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def _health_dump():
    result = run(["pw-dump"], timeout=HEALTH_TIMEOUT_SECONDS)
    if not result.ok:
        return []
    try:
        dump = json.loads(result.out)
        return dump if isinstance(dump, list) else []
    except ValueError:
        return []


def _route_health(dump, physical_name):
    """Inspect current registry objects, not the bridge's earlier READY audit."""
    node_type, port_type, link_type = ("PipeWire:Interface:" + x for x in ("Node", "Port", "Link"))
    nodes = {str(x.get("id")): x for x in dump if x.get("type") == node_type}
    ports = {str(x.get("id")): x for x in dump if x.get("type") == port_type}
    expected_names = (CAPTURE_SINK, NAMESPACE + ".capture", NAMESPACE + ".playback", physical_name)
    selected = {}
    for name in expected_names:
        matches = [x for x in nodes.values() if x.get("info", {}).get("props", {}).get("node.name") == name]
        if len(matches) != 1:
            return "missing-or-duplicate-node", {"input": 0, "output": 0}
        selected[name] = matches[0]
        info = matches[0].get("info", {})
        if info.get("error") or info.get("state") == "error":
            return "failed-audio-node", {"input": 0, "output": 0}
        if info.get("props", {}).get("object.serial") is None:
            return "missing-node-identity", {"input": 0, "output": 0}
    bridge_ids = {str(selected[name]["id"]) for name in expected_names[1:3]}
    expected = {(CAPTURE_SINK, NAMESPACE + ".capture", channel) for channel in POSITIONS}
    expected.update((NAMESPACE + ".playback", physical_name, channel) for channel in ("FL", "FR"))
    seen, counts = set(), {"input": 0, "output": 0}
    for item in dump:
        if item.get("type") != link_type:
            continue
        info = item.get("info", {})
        if not any(str(info.get(side + "-node-id")) in bridge_ids for side in ("input", "output")):
            continue
        if info.get("error") or info.get("state") not in ("active", "paused"):
            return "inactive-or-failed-link", counts
        endpoints = []
        for side in ("output", "input"):
            node_id, port_id = str(info.get(side + "-node-id")), str(info.get(side + "-port-id"))
            node, port = nodes.get(node_id), ports.get(port_id)
            if node is None or port is None:
                return "missing-link-endpoint", counts
            port_info, props = port.get("info", {}), port.get("info", {}).get("props", {})
            if (str(props.get("node.id")) != node_id or port_info.get("direction") != side
                    or props.get("object.serial") is None):
                return "invalid-port-identity", counts
            endpoints.append((node.get("info", {}).get("props", {}).get("node.name"), props.get("audio.channel")))
        (output_name, output_channel), (input_name, input_channel) = endpoints
        route = (output_name, input_name, output_channel)
        if output_channel != input_channel or route not in expected:
            return "unexpected-channel-route", counts
        if route in seen:
            return "duplicate-channel-route", counts
        seen.add(route)
        counts["input" if input_name == NAMESPACE + ".capture" else "output"] += 1
    return (None if seen == expected else "incomplete-channel-routes"), counts


def _health_snapshot(dump, state):
    def result(healthy, available, reason, links=None):
        return {"healthy": healthy, "available": available, "reason": reason,
                "links": links or {"input": 0, "output": 0}}
    if not dump:
        return result(False, False, "pipewire-unavailable")
    target = hardware.find_speaker_sink(dump)
    if not target:
        return result(False, False, "physical-output-unavailable")
    default = run(["pactl", "get-default-sink"], timeout=HEALTH_TIMEOUT_SECONDS)
    if not default.ok or not default.out.strip():
        return result(False, False, "audio-session-unavailable")
    # An explicitly selected HDMI/USB/Bluetooth output is not a resume failure.
    # Recovery must not take the user's new output back to the analog speakers.
    if default.out.strip() not in (CAPTURE_SINK, target["name"]):
        return result(False, False, "external-output-selected")
    service = converted_runtime.systemctl("show", "--property=ActiveState", "--property=MainPID",
                                          paths.UNIT_NAME, timeout=HEALTH_TIMEOUT_SECONDS)
    properties = dict(line.split("=", 1) for line in service.out.splitlines() if "=" in line)
    if not service.ok or properties.get("ActiveState") != "active":
        return result(False, True, "service-inactive")
    pid = state.get("daemon_pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or properties.get("MainPID") != str(pid):
        return result(False, True, "daemon-identity-mismatch")
    if not state.get("ready") or state.get("state") not in ("ready", "running") or state.get("last_error"):
        return result(False, True, "worker-not-ready")
    updated = state.get("updated_unix_seconds")
    if (isinstance(updated, bool) or not isinstance(updated, (int, float)) or not math.isfinite(updated)
            or not -2 <= time.time() - updated <= HEARTBEAT_MAX_AGE_SECONDS):
        return result(False, True, "stale-worker-heartbeat")
    journal = read_json(ROUTING_FILE, {}) or {}
    if journal.get("restored") or not journal.get("routed") or journal.get("physical_sink") != target["name"]:
        return result(False, True, "routing-not-applied")
    # ALSA node names survive device recreation; registry serials do not. A
    # complete set of new links must not reuse the old session's volume journal.
    ownership = {"ally-dsp-original-routing-v2": ("master_mirror", "ally-dsp-original-physical-master-v1"),
                 "ally-dsp-original-routing-v1": ("volume_handoff", "ally-dsp-original-volume-handoff-v1")}
    owner_key, owner_schema = ownership.get(journal.get("schema"), (None, None))
    owner = journal.get(owner_key)
    identity = (owner.get("physical_identity")
                if isinstance(owner, dict) and owner.get("schema") == owner_schema else None)
    serial = identity.get("serial") if isinstance(identity, dict) else None
    if (not isinstance(identity, dict) or identity.get("name") != target["name"]
            or isinstance(serial, bool) or not isinstance(serial, (str, int)) or not str(serial)):
        return result(False, False, "routing-identity-unavailable")
    physical = next((x for x in dump if x.get("type") == "PipeWire:Interface:Node"
                     and x.get("id") == target["id"]), {})
    current_serial = physical.get("info", {}).get("props", {}).get("object.serial")
    if current_serial is None:
        return result(False, False, "physical-identity-unavailable")
    if str(current_serial) != str(serial):
        return result(False, True, "physical-output-recreated")
    if (state.get("capture_sink") != CAPTURE_SINK or state.get("playback_sink") != target["name"]
            or not state.get("node_routes_verified")):
        return result(False, True, "worker-route-mismatch")
    owned = [x for x in dump if x.get("type") == "PipeWire:Interface:Node"
             and x.get("info", {}).get("props", {}).get("node.name") == CAPTURE_SINK]
    if not owned:
        return result(False, True, "capture-sink-unavailable")
    if len(owned) != 1 or not journal.get("token") or owned[0].get("info", {}).get("props", {}).get("allydsp.session") != journal["token"]:
        return result(False, False, "capture-sink-ownership-changed")
    if default.out.strip() != CAPTURE_SINK:
        return result(False, True, "default-bypasses-original")
    reason, links = _route_health(dump, target["name"])
    return result(reason is None, True, reason or "ready", links)


def health() -> Dict[str, Any]:
    """Bounded, read-only liveness and channel-route check for session recovery.

    ``available`` describes whether analog recovery can currently be attempted;
    ``healthy`` also requires this daemon's fresh heartbeat and complete routes.
    No service, default output, journal, volume or mute state is changed here.
    """
    return _health_snapshot(_health_dump(), read_json(STATUS_FILE, {}) or {})


def active_meta():
    return read_json(META_FILE)


def validate_output_gain(value):
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 12.0:
        raise ValueError("Original output boost must be between 0 and 12 dB")
    return value


def output_gain_db():
    return validate_output_gain(settings.load().get("original", {}).get("outputGainDb", 0.0))


def apply_output_gain(value=None):
    """Set only this service's playback stream; leave device/game volumes alone.

    PulseAudio's integer volume is cubic: 65536 * 10**(dB/60). An absolute
    integer avoids cumulative relative '+dB' changes on repeated starts.
    This is a playback-level control, separate from the original DLL settings.
    """
    gain = output_gain_db() if value is None else validate_output_gain(value)
    if not verify(0):
        return {"outputGainDb": gain, "applied": False}
    journal = read_json(ROUTING_FILE, {}) or {}
    sinks = _sinks()
    physical = next((x for x in sinks if x.get("name") == journal.get("physical_sink")), None)
    candidates = [x for x in _inputs()
                  if x.get("properties", {}).get("node.name") == NAMESPACE + ".playback"]
    if len(candidates) != 1 or not physical or candidates[0].get("sink") != physical["index"]:
        raise RuntimeError("The owned original playback stream is unavailable")
    volume = str(round(65536 * 10 ** (gain / 60.0)))
    _checked(["pactl", "set-sink-input-volume", str(candidates[0]["index"]), volume])
    meta = active_meta()
    if isinstance(meta, dict):
        meta["outputGainDb"] = gain
        try:
            write_json(META_FILE, meta)
        except OSError as error:
            logger.warning("Could not update original output-level metadata: %s", error)
    return {"outputGainDb": gain, "applied": True}


def apply_preset(profile: str, voicing: str, pregain_db: float = 0.0, restart_unit: bool = True) -> Dict[str, Any]:
    config = _configuration()
    # Original assets carry an explicit verified profile; converted voicings do
    # not silently become unsupported original-DLL parameters.
    active = {"engine": "original", "profile": config.get("profile", "game"),
              "voicing": config.get("voicing", "balanced"), "preGainDb": 0.0, "outputGainDb": output_gain_db(),
              "requestedConvertedPreset": {"profile": profile, "voicing": voicing, "preGainDb": pregain_db},
              "engineLabel": config.get("label"), "engineProfile": config["engine_profile"],
              "scope": config.get("scope"), "applied_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    os.makedirs(ORIGINAL_DIR, exist_ok=True)
    write_json(META_FILE, active)
    if restart_unit:
        start()
    active["verified"] = verify(0)
    return active


def status(with_journal: bool = True) -> Dict[str, Any]:
    state = read_json(STATUS_FILE, {}) or {}
    dump = _health_dump()
    names = {item.get("id"): item.get("info", {}).get("props", {}).get("node.name")
             for item in dump if item.get("type") == "PipeWire:Interface:Node"}
    links = {"input": 0, "output": 0}
    for item in dump:
        if item.get("type") != "PipeWire:Interface:Link":
            continue
        info = item.get("info", {})
        if names.get(info.get("input-node-id")) == NAMESPACE + ".capture":
            links["input"] += 1
        if names.get(info.get("output-node-id")) == NAMESPACE + ".playback":
            links["output"] += 1
    current_health = _health_snapshot(dump, state)
    return {"unit_installed": os.path.isfile(paths.UNIT_PATH), "unit_current": unit_installed(),
            "enabled": converted_runtime.is_enabled(), "active": converted_runtime.is_active(),
            "verified": current_health["healthy"], "health": current_health,
            "links": links, "active_preset": active_meta(),
            "journal": converted_runtime.journal(12) if with_journal else "", "original": state}
