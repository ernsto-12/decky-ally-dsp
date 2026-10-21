"""Original-DLL runtime lifecycle and reversible routing, in the user's session.

The service name is shared with the converted engine. It is never a second
audio service running alongside it. Private DLLs and tuning remain in data/.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any, Dict

from . import converted_runtime, hardware, paths
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


def _restore_routing_locked() -> None:
    journal = read_json(ROUTING_FILE)
    if not isinstance(journal, dict) or journal.get("schema") != "ally-dsp-original-routing-v1" or journal.get("restored"):
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
    previous = _checked(["pactl", "get-default-sink"])
    if previous == CAPTURE_SINK:
        raise RuntimeError("A previous original-engine route needs recovery")
    os.makedirs(ORIGINAL_DIR, exist_ok=True)
    journal = {"schema": "ally-dsp-original-routing-v1", "previous_default": previous,
               "physical_sink": target["name"], "module_id": None, "moved": [], "restored": False,
               "token": uuid.uuid4().hex}
    write_json(ROUTING_FILE, journal)
    module = _checked(["pactl", "load-module", "module-null-sink", "sink_name=" + CAPTURE_SINK,
                           "rate=48000", "channels=6", "channel_map=front-left,front-right,front-center,lfe,rear-left,rear-right",
                           'sink_properties="device.description=AllyDSPOriginal node.virtual=true priority.session=0 channelmix.upmix=false allydsp.session=' + journal["token"] + '"'])
    if not module.isdigit():
        raise RuntimeError("PipeWire returned an invalid sink module identifier")
    journal["module_id"] = int(module)
    write_json(ROUTING_FILE, journal)


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


def active_meta():
    return read_json(META_FILE)


def apply_preset(profile: str, voicing: str, pregain_db: float = 0.0, restart_unit: bool = True) -> Dict[str, Any]:
    config = _configuration()
    # Original assets carry an explicit verified profile; converted voicings do
    # not silently become unsupported original-DLL parameters.
    active = {"engine": "original", "profile": config.get("profile", "game"),
              "voicing": config.get("voicing", "balanced"), "preGainDb": 0.0,
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
    return {"unit_installed": os.path.isfile(paths.UNIT_PATH), "unit_current": unit_installed(),
            "enabled": converted_runtime.is_enabled(), "active": converted_runtime.is_active(),
            "verified": verify(0), "links": {"input": len(state.get("capture_links", [])),
                                             "output": len(state.get("playback_links", []))}, "active_preset": active_meta(),
            "journal": converted_runtime.journal(12) if with_journal else "", "original": state}
