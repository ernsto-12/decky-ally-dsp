"""Profile selection may change DSP controls, never its immutable launch ownership."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from allydsp import original_runtime as runtime, original_stop as stop
from test_original_stop import private_root, session  # Reuse the owned-prefix fixture.


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def configured(tmp_path, monkeypatch):
    directory = tmp_path / "original"
    directory.mkdir()
    for name, basename in (("ORIGINAL_DIR", None), ("ENGINE_FILE", "engine.json"),
                           ("RUNTIME_FILE", "runtime.json"), ("META_FILE", "active.json"),
                           ("STATUS_FILE", "status.json"), ("CONTROLS_FILE", "controls.json")):
        monkeypatch.setattr(runtime, name, str(directory if basename is None else directory / basename))
    plugin = tmp_path / "plugin"
    bridge = plugin / "py_modules/allydsp/original/pipewire_bridge.py"
    bridge.parent.mkdir(parents=True)
    bridge.write_bytes(b"vendor-free bridge fixture")
    monkeypatch.setattr(runtime.paths, "PLUGIN_DIR", str(plugin))
    monkeypatch.setattr(runtime.hardware, "pw_dump", lambda: [])
    monkeypatch.setattr(runtime.hardware, "codec_info", lambda: {
        "ssid": "10431384", "dev": "0294", "supported": True})
    monkeypatch.setattr(runtime.hardware, "output_route", lambda _: {"route": "speaker"})
    monkeypatch.setattr(runtime.hardware, "headphones_active", lambda _: False)
    worker = {"command": ["/private/proton", "runinprefix", "private-worker", "--preset", "game",
                           "--voicing", "balanced", "--dll", "private-original.dll"],
              "environment": {"WINEPREFIX": "/private/compat/pfx"}, "cwd": "/private/generation"}
    row = {"cp": {}, "init": {}, "voicings": {}, "post_plan": {"sha256": "a" * 64}}
    catalog = {"schema": "ally-dsp-original-profiles-v1", "routes": {
        route: {"profiles": {preset: copy.deepcopy(row) for preset in ("game", "movie")}}
        for route in ("speakers", "headphones")}}
    catalog_path = directory / "catalog.json"
    write(catalog_path, catalog)
    manifest = {"schema": "ally-dsp-original-engine-v2", "generation": "b" * 64,
                "sample_rate": 48000, "input_channels": 6, "output_channels": 2,
                "supported_presets": ["game", "movie"], "supported_voicings": ["balanced", "warm"],
                "profile_catalog": {"path": str(catalog_path), "sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest()},
                "profiles": {route: {"worker": copy.deepcopy(worker), "profile": "game", "voicing": "balanced"}
                             for route in ("speakers", "headphones")},
                "files": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                          for path in (catalog_path, bridge)]}
    write(Path(runtime.ENGINE_FILE), manifest)
    return manifest, worker


def test_v2_selection_is_a_runtime_copy_and_does_not_modify_manifest(configured):
    manifest, _ = configured
    before = Path(runtime.ENGINE_FILE).read_bytes()
    selected = runtime._configuration("movie", "warm")
    command = selected["worker"]["command"]
    assert selected["engine_profile"] == "speakers"
    assert selected["profile"] == "movie" and selected["voicing"] == "warm"
    assert command[command.index("--preset") + 1] == "movie"
    assert command[command.index("--voicing") + 1] == "warm"
    assert selected["runtime_switchable"] is True
    assert Path(runtime.ENGINE_FILE).read_bytes() == before
    assert json.loads(before) == manifest
    assert runtime._configuration("game", "balanced")["worker"] == manifest["profiles"]["speakers"]["worker"]


@pytest.mark.parametrize("preset,voicing", [("../../movie", "warm"), ("movie", "unknown"), ("other", "balanced")])
def test_unknown_profile_selection_fails_before_a_unit_or_route_change(configured, monkeypatch, preset, voicing):
    monkeypatch.setattr(runtime, "ensure_unit", lambda: pytest.fail("invalid selection must not mutate a service"))
    with pytest.raises(ValueError, match="Unsupported original"):
        runtime._configuration(preset, voicing)


@pytest.mark.parametrize("change", ["dll", "prefix", "cwd", "extra-argument", "missing-selection", "duplicate-selection"])
def test_hot_compatibility_does_not_mask_changes_to_dll_prefix_or_command(configured, change):
    _, original = configured
    changed = copy.deepcopy(original)
    changed["command"][changed["command"].index("--preset") + 1] = "movie"
    if change == "dll":
        changed["command"][-1] = "other.dll"
    elif change == "prefix":
        changed["environment"]["WINEPREFIX"] = "/Steam/compatdata/a-game/pfx"
    elif change == "cwd":
        changed["cwd"] = "/foreign/generation"
    elif change == "extra-argument":
        changed["command"].append("--unreviewed")
    elif change == "missing-selection":
        changed["command"] = [arg for arg in changed["command"] if arg != "--preset"]
    else:
        changed["command"].extend(["--preset", "game"])
    assert runtime._same_worker_base(original, changed) is False


def test_same_route_hot_profile_switch_preserves_unit_runtime_and_routes(configured, monkeypatch):
    manifest, worker = configured
    previous_runtime = {"engine_profile": "speakers", "worker": worker}
    write(Path(runtime.RUNTIME_FILE), previous_runtime)
    before = Path(runtime.RUNTIME_FILE).read_bytes()
    calls = []
    monkeypatch.setattr(runtime.converted_runtime, "is_active", lambda: True)
    monkeypatch.setattr(runtime, "verify", lambda *_: True)
    monkeypatch.setattr(runtime.settings, "load", lambda: {"original": {"controls": {"eqEnabled": True}}})
    def apply(controls, *, preset, voicing):
        calls.append((controls, preset, voicing))
        return {"applied": True}
    monkeypatch.setattr(runtime, "apply_controls", apply)
    monkeypatch.setattr(runtime, "ensure_unit", lambda: pytest.fail("hot switch must not rewrite the unit"))
    monkeypatch.setattr(runtime.converted_runtime, "stop", lambda: pytest.fail("hot switch must not stop the unit"))
    monkeypatch.setattr(runtime, "restore_routing", lambda: pytest.fail("hot switch must not touch routing"))
    assert runtime.start(runtime._configuration("movie", "warm")) is True
    assert calls == [({"eqEnabled": True}, "movie", "warm")]
    assert Path(runtime.RUNTIME_FILE).read_bytes() == before
    assert json.loads(Path(runtime.ENGINE_FILE).read_text(encoding="utf-8")) == manifest


@pytest.mark.parametrize("codec", [None,
    {"ssid": "10431eb3", "dev": "0294", "supported": True},
    {"ssid": "10431394", "dev": "0294", "supported": True},
    {"ssid": "10431384", "dev": "1234", "supported": True},
    {"ssid": "10431384", "dev": "0294", "supported": False},
])
def test_unverified_hardware_cannot_start_or_expose_the_rc73xa_engine(monkeypatch, codec):
    monkeypatch.setattr(runtime.hardware, "codec_info", lambda: codec)
    monkeypatch.setattr(runtime, "_configuration", lambda *_: pytest.fail("unsupported hardware must fail before loading its pack"))
    monkeypatch.setattr(runtime, "ensure_unit", lambda: pytest.fail("unsupported hardware must not change a unit"))
    monkeypatch.setattr(runtime.converted_runtime, "start", lambda: pytest.fail("unsupported hardware must not start audio"))
    availability = runtime.availability()
    assert availability["available"] is False
    assert "RC73XA" in availability["error"]
    with pytest.raises(RuntimeError, match="RC73XA"):
        runtime.start()


def test_verified_hardware_identifier_normalization_is_accepted(monkeypatch):
    codec = {"ssid": "0x10431384", "dev": "0x0294", "supported": True}
    monkeypatch.setattr(runtime.hardware, "codec_info", lambda: codec)
    assert runtime._validate_hardware() is codec


def _v2_stop_selection(session):
    session.manifest.update(schema="ally-dsp-original-engine-v2", supported_presets=["game", "movie"],
                            supported_voicings=["balanced", "warm"])
    worker = session.manifest["profiles"]["speakers"]["worker"]
    worker["command"].extend(["--preset", "game", "--voicing", "balanced"])
    session.runtime["worker"] = copy.deepcopy(worker)
    actual = session.runtime["worker"]["command"]
    actual[actual.index("--preset") + 1] = "movie"
    actual[actual.index("--voicing") + 1] = "warm"
    return actual


def test_v2_stop_ownership_accepts_only_known_launch_selection_changes(session):
    _v2_stop_selection(session)
    session.write()
    plan = stop._private_prefix_plan()
    assert plan["prefix"] == str(session.prefix)
    assert plan["generation"] == session.generation
    assert session.signals == session.calls == []


@pytest.mark.parametrize("change", ["unknown-preset", "unknown-voicing", "extra-argument", "duplicate-selection", "foreign-prefix"])
def test_v2_stop_does_not_relax_private_prefix_ownership(session, change):
    command = _v2_stop_selection(session)
    if change == "unknown-preset":
        command[command.index("--preset") + 1] = "foreign"
    elif change == "unknown-voicing":
        command[command.index("--voicing") + 1] = "foreign"
    elif change == "extra-argument":
        command.append("--unreviewed")
    elif change == "duplicate-selection":
        command.extend(["--preset", "game"])
    else:
        session.runtime["worker"]["environment"]["WINEPREFIX"] = str(session.root / "some-game/pfx")
    session.write()
    with pytest.raises(RuntimeError):
        stop._private_prefix_plan()
    assert session.signals == session.calls == []
