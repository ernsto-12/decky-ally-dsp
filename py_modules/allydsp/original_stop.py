"""Bounded shutdown of this engine's private Proton prefix, with safe fallback.

No PID from a status file is trusted. Any failed ownership check leaves process
termination to the unit's existing KillMode and timeout; routing still recovers.
"""
from pathlib import Path
import os
import select
import signal

from . import original_runtime as runtime, paths
from .log import logger
from .util import read_json, run, user_env


GRACE_SECONDS = 0.75
PREFIX_PROBE_SECONDS = 0.1
PREFIX_KILL_SECONDS = 1.0
PREFIX_DRAIN_SECONDS = 0.25


def _plain_path(path):
    """Require an existing canonical path; never follow a swapped prefix link."""
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path.absolute():
        raise RuntimeError("Original stop path is not a canonical private path")
    return path


def _windows_path(path):
    return "Z:" + path.as_posix().replace("/", "\\")


def _private_prefix_plan():
    original = _plain_path(runtime.ORIGINAL_DIR)
    _plain_path(runtime.ENGINE_FILE)
    _plain_path(runtime.RUNTIME_FILE)
    manifest_hash = runtime._hash_file(runtime.ENGINE_FILE)
    runtime_hash = runtime._hash_file(runtime.RUNTIME_FILE)
    config, active = read_json(runtime.ENGINE_FILE), read_json(runtime.RUNTIME_FILE)
    if not isinstance(config, dict) or config.get("schema") != "ally-dsp-original-engine-v1":
        raise RuntimeError("Original stop manifest is unavailable")
    generation = config.get("generation")
    if (not isinstance(generation, str) or len(generation) != 64
            or any(char not in "0123456789abcdef" for char in generation)):
        raise RuntimeError("Original stop generation is invalid")
    if (not isinstance(active, dict) or active.get("schema") != "original-dll-pipewire-bridge-v1"
            or active.get("namespace") != runtime.NAMESPACE or active.get("capture_sink") != runtime.CAPTURE_SINK):
        raise RuntimeError("Original stop runtime is not the private bridge")
    profiles = config.get("profiles")
    selected = profiles.get(active.get("engine_profile")) if isinstance(profiles, dict) else None
    worker = selected.get("worker") if isinstance(selected, dict) else None
    if not isinstance(worker, dict) or active.get("worker") != worker:
        raise RuntimeError("Original stop runtime and current generation disagree")
    target = _plain_path(original / "assets" / generation)
    compat = _plain_path(original / "compatdata" / generation)
    prefix = _plain_path(compat / "pfx")
    command, environment = worker.get("command"), worker.get("environment")
    if (not isinstance(command, list) or len(command) < 6
            or not all(isinstance(arg, str) and "\0" not in arg for arg in command)
            or not isinstance(environment, dict) or worker.get("cwd") != str(target)
            or environment.get("STEAM_COMPAT_DATA_PATH") != str(compat)
            or environment.get("WINEPREFIX") != str(prefix)
            or environment.get("STEAM_COMPAT_INSTALL_PATH") != str(target)
            or command[1:6] != ["runinprefix", _windows_path(target / "python-embed/python.exe"),
                                "-u", "-B", _windows_path(target / "worker/windows_worker.py")]):
        raise RuntimeError("Original stop worker is not confined to this private prefix")
    proton = _plain_path(command[0])
    if not proton.is_file():
        raise RuntimeError("Original stop Proton launcher is unavailable")
    pins = config.get("files")
    if not isinstance(pins, list) or not pins:
        raise RuntimeError("Original stop file pins are unavailable")
    verified = set()
    for item in pins:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise RuntimeError("Original stop manifest has an invalid file pin")
        path = _plain_path(item["path"])
        if not path.is_file() or str(path) in verified or runtime._hash_file(str(path)) != item.get("sha256"):
            raise RuntimeError("Original stop engine files have changed")
        verified.add(str(path))
    bridge = Path(paths.PLUGIN_DIR) / "py_modules/allydsp/original/pipewire_bridge.py"
    required = (proton, target / "python-embed/python.exe", target / "worker/windows_worker.py", bridge)
    if any(str(path) not in verified for path in required):
        raise RuntimeError("Original stop manifest does not pin its launcher and workers")
    # Proton may ship a wineserver symlink; its real executable must stay inside
    # the same pinned launcher's distribution, never another Wine installation.
    server = (proton.parent / "files/bin/wineserver").resolve(strict=True)
    if not server.is_relative_to(proton.parent) or not server.is_file() or not os.access(server, os.X_OK):
        raise RuntimeError("Original stop wineserver is outside this Proton installation")
    server_hash = runtime._hash_file(str(server))
    if (runtime._hash_file(runtime.ENGINE_FILE) != manifest_hash
            or runtime._hash_file(runtime.RUNTIME_FILE) != runtime_hash):
        raise RuntimeError("Original stop configuration changed while validating assets")
    return {"server": str(server), "prefix": str(prefix), "bridge": str(bridge), "generation": generation,
            "manifest_hash": manifest_hash, "runtime_hash": runtime_hash, "server_hash": server_hash}


def _stop_context():
    service = runtime.converted_runtime.systemctl("show", "--property=ActiveState", "--property=SubState",
                                                "--property=ControlPID", "--property=MainPID", paths.UNIT_NAME, timeout=1)
    properties = dict(line.split("=", 1) for line in service.out.splitlines() if "=" in line)
    if (not service.ok or properties.get("ActiveState") != "deactivating" or properties.get("SubState") != "stop"
            or properties.get("ControlPID") != str(os.getpid()) or not properties.get("MainPID", "").isdigit()):
        raise RuntimeError("Original fast stop is only permitted inside this unit's ExecStop")
    return int(properties["MainPID"])


def _request_bridge_stop(plan, pid):
    if pid == 0:
        return None  # The bridge already exited; its private prefix can linger.
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("Original stop requires PID-safe Linux signalling")
    try:
        descriptor = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        return None
    try:
        with open("/proc/" + str(pid) + "/cmdline", "rb") as stream:
            command = [os.fsdecode(arg) for arg in stream.read(65536).split(b"\0") if arg]
        expected = [paths.SYSTEM_PYTHON, plan["bridge"], "--config", runtime.RUNTIME_FILE]
        executable = os.path.realpath(os.readlink("/proc/" + str(pid) + "/exe"))
        if command != expected or executable != os.path.realpath(paths.SYSTEM_PYTHON):
            raise RuntimeError("Original stop MainPID is not the configured Python bridge")
        signal.pidfd_send_signal(descriptor, signal.SIGTERM, None, 0)
        return descriptor
    except (FileNotFoundError, ProcessLookupError):
        os.close(descriptor)
        return None  # Valid ExecStop context; the former MainPID has exited.
    except BaseException:
        os.close(descriptor)
        raise


def _check_plan_current(plan):
    if (runtime._hash_file(runtime.ENGINE_FILE) != plan["manifest_hash"]
            or runtime._hash_file(runtime.RUNTIME_FILE) != plan["runtime_hash"]
            or runtime._hash_file(plan["server"]) != plan["server_hash"]):
        raise RuntimeError("Original stop generation changed during shutdown")
    _plain_path(plan["prefix"])


def _drain_prefix(plan):
    _check_plan_current(plan)
    env = user_env({"WINEPREFIX": plan["prefix"], "WINEDEBUG": "-all"})
    probe = run([plan["server"], "-w"], timeout=PREFIX_PROBE_SECONDS, env=env)
    if probe.ok:
        return "already-stopped"
    if probe.rc != 124:
        raise RuntimeError("Original stop prefix liveness check failed")
    _check_plan_current(plan)
    killed = run([plan["server"], "-k"], timeout=PREFIX_KILL_SECONDS, env=env)
    if not killed.ok:
        raise RuntimeError("Original stop private prefix did not accept shutdown")
    drained = run([plan["server"], "-w"], timeout=PREFIX_DRAIN_SECONDS, env=env)
    return "stopped" if drained.ok else "systemd-fallback"


def stop_locked():
    """ExecStop action, called with the routing lock; always restore routes."""
    descriptor = None
    outcome = {"daemon_signalled": False, "prefix_cleanup": "systemd-fallback"}
    try:
        pid = _stop_context()
        plan = _private_prefix_plan()
        descriptor = _request_bridge_stop(plan, pid)
        outcome["daemon_signalled"] = descriptor is not None
        if descriptor is not None:
            select.select([descriptor], [], [], GRACE_SECONDS)
        outcome["prefix_cleanup"] = _drain_prefix(plan)
    except Exception as error:
        logger.warning("Original audio fast stop deferred to systemd: %s", error)
        outcome["error"] = str(error)
    finally:
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            runtime._restore_routing_locked()
    logger.info("Original audio stop: signalled=%s, prefix=%s", outcome["daemon_signalled"], outcome["prefix_cleanup"])
    return outcome
