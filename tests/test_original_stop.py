"""Private Wine cleanup must never target games, foreign PIDs or stale assets."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile

import pytest

from allydsp import original_runtime as runtime, original_stop as stop
from allydsp.util import Result


@pytest.fixture
def private_root():
    with tempfile.TemporaryDirectory(prefix="original-stop-test-", dir=Path(__file__).parent) as directory:
        yield Path(directory).resolve()


@pytest.fixture
def session(private_root, monkeypatch):
    class Session:
        signals = []
        closes = []
        calls = []
        restored = 0
        service_state = "deactivating"
        sub_state = "stop"
        control_pid = os.getpid()
        main_pid = 42
        results = [Result(124, "", "timeout"), Result(0, "", ""), Result(0, "", "")]

        def systemctl(self, *args, timeout):
            assert args == ("show", "--property=ActiveState", "--property=SubState", "--property=ControlPID",
                            "--property=MainPID", runtime.paths.UNIT_NAME)
            assert timeout == 1
            return Result(0, "ActiveState=" + self.service_state + "\nSubState=" + self.sub_state
                          + "\nControlPID=" + str(self.control_pid) + "\nMainPID=" + str(self.main_pid) + "\n", "")

        def run(self, argv, *, env, timeout):
            assert argv[0] == str(self.server)
            assert env["WINEPREFIX"] == str(self.prefix)
            assert "SteamAppId" not in env  # No ambient game/prefix environment.
            self.calls.append((argv, timeout))
            return self.results.pop(0)

        def restore(self):
            self.restored += 1

        def write(self):
            self.engine.write_text(json.dumps(self.manifest), encoding="utf-8")
            self.active.write_text(json.dumps(self.runtime), encoding="utf-8")

        def proc_open(self, path, mode="r", *args, **kwargs):
            if str(path) == "/proc/42/cmdline":
                return io.BytesIO(b"\0".join(os.fsencode(arg) for arg in self.command) + b"\0")
            return self.real_open(path, mode, *args, **kwargs)

    s = Session()
    s.root = private_root
    s.original = s.root / "original"
    s.generation = "a" * 64
    s.target = s.original / "assets" / s.generation
    s.compat = s.original / "compatdata" / s.generation
    s.prefix = s.compat / "pfx"
    s.prefix.mkdir(parents=True)
    s.plugin = s.root / "plugin"
    s.bridge = s.plugin / "py_modules/allydsp/original/pipewire_bridge.py"
    s.proton = s.root / "Proton 11.0/proton"
    s.server = s.proton.parent / "files/bin/wineserver"
    s.python = s.root / "system-python"
    files = [s.proton, s.target / "python-embed/python.exe", s.target / "worker/windows_worker.py", s.bridge]
    for file in files + [s.server, s.python]:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"vendor-free fixture: " + file.name.encode())
        file.chmod(0o700)
    worker = {"cwd": str(s.target), "environment": {"WINEPREFIX": str(s.prefix),
              "STEAM_COMPAT_DATA_PATH": str(s.compat), "STEAM_COMPAT_INSTALL_PATH": str(s.target)},
              "command": [str(s.proton), "runinprefix", stop._windows_path(s.target / "python-embed/python.exe"),
                          "-u", "-B", stop._windows_path(s.target / "worker/windows_worker.py"), "--engine", "hrtf-speakers"]}
    s.manifest = {"schema": "ally-dsp-original-engine-v1", "generation": s.generation,
                  "profiles": {"speakers": {"worker": worker}}, "files": [
                      {"path": str(file), "sha256": hashlib.sha256(file.read_bytes()).hexdigest()} for file in files]}
    s.runtime = {"schema": "original-dll-pipewire-bridge-v1", "namespace": runtime.NAMESPACE,
                 "capture_sink": runtime.CAPTURE_SINK, "engine_profile": "speakers", "worker": copy.deepcopy(worker)}
    s.engine, s.active = s.original / "engine.json", s.original / "runtime.json"
    s.write()
    s.command = [str(s.python), str(s.bridge), "--config", str(s.active)]
    monkeypatch.setattr(runtime, "ORIGINAL_DIR", str(s.original))
    monkeypatch.setattr(runtime, "ENGINE_FILE", str(s.engine))
    monkeypatch.setattr(runtime, "RUNTIME_FILE", str(s.active))
    monkeypatch.setattr(runtime.paths, "PLUGIN_DIR", str(s.plugin))
    monkeypatch.setattr(runtime.paths, "SYSTEM_PYTHON", str(s.python))
    monkeypatch.setattr(runtime.converted_runtime, "systemctl", s.systemctl)
    monkeypatch.setattr(runtime, "_restore_routing_locked", s.restore)
    monkeypatch.setattr(stop, "run", s.run)
    monkeypatch.setattr(stop, "user_env", lambda extra: dict(extra))
    monkeypatch.setattr(stop.os, "pidfd_open", lambda pid, flags: 123, raising=False)
    monkeypatch.setattr(stop.signal, "pidfd_send_signal", lambda fd, sig, _info, flags: s.signals.append((fd, sig)), raising=False)
    monkeypatch.setattr(stop.os, "close", lambda fd: s.closes.append(fd))
    real_readlink = os.readlink
    monkeypatch.setattr(stop.os, "readlink", lambda path, *args, **kwargs: str(s.python) if path == "/proc/42/exe"
                        else real_readlink(path, *args, **kwargs))
    monkeypatch.setattr(stop.select, "select", lambda read, write, error, timeout: (read, [], []))
    import builtins
    s.real_open = builtins.open
    monkeypatch.setattr(builtins, "open", s.proc_open)
    return s


def test_normal_stop_signals_only_validated_main_then_drains_private_prefix_and_restores(session):
    result = stop.stop_locked()
    assert result == {"daemon_signalled": True, "prefix_cleanup": "stopped"}
    assert session.signals == [(123, stop.signal.SIGTERM)]
    assert session.closes == [123]
    assert [(args[1], timeout) for args, timeout in session.calls] == [("-w", .1), ("-k", 1.0), ("-w", .25)]
    assert session.restored == 1


def test_dead_bridge_still_cleans_owned_prefix_in_valid_systemd_stop_context(session):
    session.main_pid = 0
    result = stop.stop_locked()
    assert result["prefix_cleanup"] == "stopped"
    assert session.signals == []
    assert session.restored == 1


@pytest.mark.parametrize("missing_at", ["pidfd", "proc"])
def test_main_that_exited_during_stop_does_not_leave_lingering_wine_children(session, monkeypatch, missing_at):
    if missing_at == "pidfd":
        monkeypatch.setattr(stop.os, "pidfd_open", lambda *args: (_ for _ in ()).throw(ProcessLookupError()))
    else:
        def missing_proc(path, *args, **kwargs):
            if str(path) == "/proc/42/cmdline":
                raise FileNotFoundError()
            return session.real_open(path, *args, **kwargs)
        import builtins
        monkeypatch.setattr(builtins, "open", missing_proc)
    result = stop.stop_locked()
    assert result["prefix_cleanup"] == "stopped"
    assert session.signals == []
    assert session.restored == 1


def test_cleanly_exited_prefix_needs_no_kill(session):
    session.results = [Result(0, "", "")]
    assert stop.stop_locked()["prefix_cleanup"] == "already-stopped"
    assert [args[1] for args, _ in session.calls] == ["-w"]


@pytest.mark.parametrize("foreign", ["control", "active", "substate", "main-command", "main-executable"])
def test_foreign_stop_context_or_live_main_never_signals_or_kills_wine(session, monkeypatch, foreign):
    if foreign == "control":
        session.control_pid += 1
    elif foreign == "active":
        session.service_state = "active"
    elif foreign == "substate":
        session.sub_state = "stop-post"
    elif foreign == "main-command":
        session.command = ["monsterhunterwilds.exe"]
    else:
        monkeypatch.setattr(stop.os, "readlink", lambda path: "some-other-executable")
    assert stop.stop_locked()["prefix_cleanup"] == "systemd-fallback"
    assert session.signals == []
    assert session.calls == []
    assert session.restored == 1


@pytest.mark.parametrize("change", ["foreign-prefix", "foreign-compat", "foreign-cwd", "runtime-generation",
                                     "changed-pin", "missing-launcher-pin", "invalid-manifest", "invalid-generation"])
def test_invalid_private_assets_and_foreign_game_paths_leave_normal_systemd_shutdown(session, change):
    worker = session.manifest["profiles"]["speakers"]["worker"]
    if change == "foreign-prefix":
        worker["environment"]["WINEPREFIX"] = str(session.root / "Steam/steamapps/compatdata/game/pfx")
    elif change == "foreign-compat":
        worker["environment"]["STEAM_COMPAT_DATA_PATH"] = str(session.root / "Steam/steamapps/compatdata/game")
    elif change == "foreign-cwd":
        worker["cwd"] = str(session.root)
    elif change == "runtime-generation":
        session.runtime["worker"]["environment"]["WINEPREFIX"] += "-old"
    elif change == "changed-pin":
        session.proton.write_bytes(b"changed launcher")
    elif change == "missing-launcher-pin":
        session.manifest["files"] = [x for x in session.manifest["files"] if x["path"] != str(session.proton)]
    elif change == "invalid-manifest":
        session.manifest["schema"] = "unknown"
    else:
        session.manifest["generation"] = "../other-prefix"
    if change.startswith("foreign-"):
        session.runtime["worker"] = copy.deepcopy(worker)
    session.write()
    assert stop.stop_locked()["prefix_cleanup"] == "systemd-fallback"
    assert session.signals == []
    assert session.calls == []
    assert session.restored == 1


def test_generation_change_after_graceful_signal_prevents_prefix_kill(session, monkeypatch):
    def wait(*args):
        session.manifest["generation"] = "b" * 64
        session.write()
        return [], [], []
    monkeypatch.setattr(stop.select, "select", wait)
    assert stop.stop_locked()["prefix_cleanup"] == "systemd-fallback"
    assert session.signals == [(123, stop.signal.SIGTERM)]
    assert session.calls == []
    assert session.restored == 1


@pytest.mark.parametrize("changed", ["manifest", "runtime"])
def test_configuration_rewritten_while_validating_assets_is_not_used_for_shutdown(session, monkeypatch, changed):
    read = stop.read_json
    def changing_read(path):
        result = read(path)
        if path == runtime.RUNTIME_FILE:
            if changed == "manifest":
                session.manifest["generation"] = "b" * 64
            else:
                session.runtime["worker"]["environment"]["WINEPREFIX"] += "-replacement"
            session.write()
        return result
    monkeypatch.setattr(stop, "read_json", changing_read)
    result = stop.stop_locked()
    assert result["prefix_cleanup"] == "systemd-fallback"
    assert "changed while validating assets" in result["error"]
    assert session.signals == []
    assert session.calls == []
    assert session.restored == 1


@pytest.mark.parametrize("result", [Result(124, "", "timeout"), Result(1, "", "failed")])
def test_wineserver_cleanup_failure_keeps_bounded_fallback_and_always_restores_routing(session, result):
    session.results = [Result(124, "", "timeout"), result]
    assert stop.stop_locked()["prefix_cleanup"] == "systemd-fallback"
    assert all(timeout <= 1 for _, timeout in session.calls)
    assert session.restored == 1


@pytest.mark.parametrize("which", ["prefix", "generation", "server-outside-proton"])
def test_symlinked_private_prefix_or_escaped_server_cannot_be_targeted(session, which):
    if which == "prefix":
        link, destination = session.prefix, session.root / "foreign-game-prefix"
    elif which == "generation":
        link, destination = session.target, session.root / "foreign-assets"
    else:
        link, destination = session.server, session.root / "foreign-wineserver"
    if link.is_dir():
        link.rename(destination)
    else:
        link.rename(destination)
    try:
        link.symlink_to(destination, target_is_directory=destination.is_dir())
    except OSError:
        pytest.skip("This host does not permit creating symbolic links")
    assert stop.stop_locked()["prefix_cleanup"] == "systemd-fallback"
    assert session.signals == []
    assert session.calls == []
    assert session.restored == 1


def test_stop_unit_uses_fast_action_but_retains_normal_systemd_kill_and_restore_fallback():
    unit = runtime.unit_text()
    assert next(line for line in unit.splitlines() if line.startswith("ExecStop=")).endswith(" --action stop")
    assert not next(line for line in unit.splitlines() if line.startswith("ExecStopPost=")).endswith(" --action stop")
    assert "KillMode=control-group" in unit
    assert "TimeoutStopSec=12" in unit
