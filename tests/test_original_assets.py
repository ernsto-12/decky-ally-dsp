"""Importer tests use generated dummy bytes; no proprietary test assets."""
import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath

import pytest

from allydsp import original_assets as assets

PUBLIC_NAMES = {"protocol.py", "windows_worker.py", "linux_client.py", "pipewire_bridge.py",
                "pipewire_graph.py", "hrtf_adapter.py", "frontend_adapter.py", "local_property_store.py", "vlldp_adapter.py"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def pack(tmp_path, monkeypatch):
    source, python, public = (tmp_path / name for name in ("source", "python", "public"))
    for directory in (source, python, public):
        directory.mkdir()
    def populate(directory, names):
        pins = {}
        for name in names:
            data = ("dummy test asset: " + name).encode()
            (directory / name).write_bytes(data)
            pins[name] = digest(data)
        return pins
    monkeypatch.setattr(assets, "VENDOR_PINS", populate(source, list(assets.VENDOR_PINS)))
    monkeypatch.setattr(assets, "PYTHON_PINS", populate(python, ["python.exe", "python312.dll", "python312.zip", "LICENSE.txt"]))
    monkeypatch.setattr(assets, "PUBLIC_PINS", populate(public, sorted(PUBLIC_NAMES)))
    steam = tmp_path / "Steam"
    proton = steam / "steamapps/common/Proton/proton"
    proton.parent.mkdir(parents=True)
    proton.write_bytes(b"#!/bin/sh\nexit 99\n")
    proton.chmod(0o755)
    runtime = tmp_path / "runtime"
    args = {"source_dir": source, "python_dir": python, "proton": proton,
            "public_dir": public, "runtime_dir": runtime}
    return args, runtime, steam


def test_import_private_generation_and_exact_worker_contract(pack):
    arguments, runtime, steam = pack
    (arguments["source_dir"] / "not-imported.dll").write_bytes(b"unrelated")
    result = assets.import_engine(**arguments)
    config = json.loads((runtime / "original/engine.json").read_text(encoding="utf-8"))
    assert result["ok"]
    assert config["schema"] == "ally-dsp-original-engine-v1"
    assert config["sample_rate"] == 48000 and config["input_channels"] == 6
    assert config["profile"] == "game" and config["voicing"] == "official-headphones"
    assert config["label"] == "Original Dolby / Auto Game"
    command = config["worker"]["command"]
    assert command[:2] == [str(arguments["proton"].resolve()), "runinprefix"]
    assert command[3] == "-u"
    assert command[command.index("--engine") + 1] == "hrtf-headphones"
    assert command[command.index("--input-channels") + 1] == "6"
    assert "Atmos object metadata are not established" in config["scope"]
    environment = config["worker"]["environment"]
    assert environment["DISPLAY"] == ":0"
    assert environment["STEAM_COMPAT_CLIENT_INSTALL_PATH"] == str(steam.resolve())
    assert Path(environment["STEAM_COMPAT_DATA_PATH"]).is_dir()
    assert environment["WINEPREFIX"] == str(Path(environment["STEAM_COMPAT_DATA_PATH"]) / "pfx")
    for record in config["files"]:
        assert Path(record["path"]).is_absolute()
        assert digest(Path(record["path"]).read_bytes()) == record["sha256"]
    assert not list((runtime / "original/assets").rglob("not-imported.dll"))
    if os.name != "nt":
        assert (runtime / "original/engine.json").stat().st_mode & 0o777 == 0o600


def test_reimport_is_idempotent_and_preserves_generation(pack):
    arguments, runtime, _ = pack
    first = assets.import_engine(**arguments)
    before = (runtime / "original/engine.json").read_bytes()
    second = assets.import_engine(**arguments)
    assert first == second
    assert (runtime / "original/engine.json").read_bytes() == before
    assert len(list((runtime / "original/assets").iterdir())) == 1


@pytest.mark.parametrize("component,name", [("source_dir", "DolbyAudioProcessing.dll"), ("source_dir", "speaker-game-supported-cp.bin"), ("source_dir", "DolbyDAXVLLDP160.dll"), ("source_dir", "speaker-game-vlldp-plan.json"), ("source_dir", "audioeng.dll"), ("public_dir", "windows_worker.py"), ("python_dir", "python312.dll")])
def test_invalid_pin_never_replaces_existing_manifest(pack, component, name):
    arguments, runtime, _ = pack
    destination = runtime / "original/engine.json"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"previous private manifest")
    (arguments[component] / name).write_bytes(b"not the verified asset")
    with pytest.raises(assets.AssetError, match="SHA256"):
        assets.import_engine(**arguments)
    assert destination.read_bytes() == b"previous private manifest"
    assert not (runtime / "original/assets").exists()


@pytest.mark.parametrize("component,name", [("source_dir", "game-init-source.bin"), ("source_dir", "speaker-game-supported-cp.bin"), ("source_dir", "DolbyDAXVLLDP160.dll"), ("source_dir", "speaker-game-vlldp-plan.json"), ("source_dir", "audioeng.dll"), ("python_dir", "python312.zip"), ("public_dir", "frontend_adapter.py")])
def test_incomplete_pack_is_rejected(pack, component, name):
    arguments, runtime, _ = pack
    (arguments[component] / name).unlink()
    with pytest.raises(assets.AssetError, match="required regular file"):
        assets.import_engine(**arguments)
    assert not (runtime / "original/engine.json").exists()


@pytest.mark.parametrize("field", ["source_dir", "python_dir", "proton", "public_dir", "runtime_dir"])
def test_relative_paths_are_rejected(pack, field):
    arguments, _, _ = pack
    arguments[field] = Path("relative/path")
    with pytest.raises(assets.AssetError, match="absolute path"):
        assets.import_engine(**arguments)


def test_parent_traversal_is_rejected(pack):
    arguments, _, _ = pack
    arguments["source_dir"] = arguments["source_dir"] / ".." / "source"
    with pytest.raises(assets.AssetError, match="parent traversal"):
        assets.import_engine(**arguments)


def _symlink_or_skip(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("Symbolic link creation is unavailable for this test user")


def test_source_file_symlink_is_rejected_even_with_correct_bytes(pack):
    arguments, runtime, _ = pack
    original = arguments["python_dir"] / "python.exe"
    other = original.with_name("copy.exe")
    other.write_bytes(original.read_bytes())
    original.unlink()
    _symlink_or_skip(original, other)
    with pytest.raises(assets.AssetError, match="regular file"):
        assets.import_engine(**arguments)
    assert not (runtime / "original/engine.json").exists()


def test_private_destination_symlink_is_rejected(pack, tmp_path):
    arguments, runtime, _ = pack
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    runtime.mkdir()
    _symlink_or_skip(runtime / "original", elsewhere, directory=True)
    with pytest.raises(assets.AssetError, match="symbolic link"):
        assets.import_engine(**arguments)
    assert not (elsewhere / "engine.json").exists()


def test_tampered_generation_is_not_overwritten(pack):
    arguments, runtime, _ = pack
    result = assets.import_engine(**arguments)
    manifest = (runtime / "original/engine.json").read_bytes()
    changed = runtime / "original/assets" / result["generation"] / "original/DolbyHrtfEnc.dll"
    changed.write_bytes(b"tampered")
    with pytest.raises(assets.AssetError, match="generation changed"):
        assets.import_engine(**arguments)
    assert changed.read_bytes() == b"tampered"
    assert (runtime / "original/engine.json").read_bytes() == manifest
    assert not list((runtime / "original/assets").glob(".import-*"))


def test_manifest_commit_failure_preserves_previous_manifest(pack, monkeypatch):
    arguments, runtime, _ = pack
    destination = runtime / "original/engine.json"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"previous")
    def fail(*_args, **_kwargs):
        raise OSError("simulated atomic manifest failure")
    monkeypatch.setattr(assets, "atomic_write_text", fail)
    with pytest.raises(OSError, match="simulated"):
        assets.import_engine(**arguments)
    assert destination.read_bytes() == b"previous"
    assert not list((runtime / "original/assets").glob(".import-*"))


def test_unexpected_module_in_generation_is_rejected(pack):
    arguments, runtime, _ = pack
    result = assets.import_engine(**arguments)
    generation = runtime / "original/assets" / result["generation"]
    (generation / "worker/unpinned.py").write_bytes(b"unreviewed")
    with pytest.raises(assets.AssetError, match="generation changed"):
        assets.import_engine(**arguments)


def test_symlink_stat_is_rejected_before_open_without_os_privileges():
    from types import SimpleNamespace
    class FakeLink:
        name = "link.dll"
        def lstat(self):
            return SimpleNamespace(st_mode=stat.S_IFLNK)
    with pytest.raises(assets.AssetError, match="regular file"):
        assets._regular_file(FakeLink())


def test_proton_windows_path_keeps_spaces_as_one_token():
    assert assets._windows_path(PurePosixPath("/home/deck/a path/worker.py")) == "Z:\\home\\deck\\a path\\worker.py"


def test_release_pin_metadata_contains_no_assets():
    assert set(assets.PUBLIC_PINS) == PUBLIC_NAMES
    assert {"python.exe", "python312.dll", "python312.zip", "LICENSE.txt"}.issubset(assets.PYTHON_PINS)
    assert len(assets.VENDOR_PINS) == 9
    for pin in [*assets.VENDOR_PINS.values(), *assets.PYTHON_PINS.values(), *assets.PUBLIC_PINS.values()]:
        assert len(pin) == 64 and all(char in "0123456789abcdef" for char in pin)


def test_release_public_source_bytes_and_internal_adapter_pins_match():
    import ast
    public = Path(assets.__file__).parent / "original"
    for name, pin in assets.PUBLIC_PINS.items():
        assert digest((public / name).read_bytes()) == pin, name
    module = ast.parse((public / "hrtf_adapter.py").read_text(encoding="utf-8"))
    pins = next(ast.literal_eval(node.value) for node in module.body
                if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "PINS" for target in node.targets))
    assert pins["adapter"] == assets.PUBLIC_PINS["frontend_adapter.py"]
    assert pins["property_store"] == assets.PUBLIC_PINS["local_property_store.py"]
    assert pins["hrtf-headphones"] == assets.VENDOR_PINS["headphone-game-output-spatial-rendering.bin"]
    assert pins["hrtf-speakers"] == assets.VENDOR_PINS["speaker-game-supported-cp.bin"]
    worker = ast.parse((public / "windows_worker.py").read_text(encoding="utf-8"))
    post_pins = next(ast.literal_eval(node.value) for node in worker.body
                    if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "POST_PINS" for target in node.targets))
    assert post_pins["adapter"] == assets.PUBLIC_PINS["vlldp_adapter.py"]
    assert post_pins["dll"] == assets.VENDOR_PINS["DolbyDAXVLLDP160.dll"]
    assert post_pins["plan"] == assets.VENDOR_PINS["speaker-game-vlldp-plan.json"]
    assert post_pins["shim"] == assets.VENDOR_PINS["audioeng.dll"]


def test_two_profiles_share_verified_assets_and_select_distinct_runtime_modes(pack):
    arguments, runtime, _ = pack
    result = assets.import_engine(**arguments)
    config = json.loads((runtime / "original/engine.json").read_text(encoding="utf-8"))
    assert result["profiles"] == ["headphones", "speakers"]
    assert set(config["profiles"]) == {"headphones", "speakers"}
    hp = config["profiles"]["headphones"]
    speaker = config["profiles"]["speakers"]
    assert config["worker"] == hp["worker"]  # Legacy readers retain the headphone default.
    assert hp["voicing"] == "official-headphones" and speaker["voicing"] == "official-speakers"
    assert hp["profile"] == speaker["profile"] == "game"
    assert hp["worker"]["cwd"] == speaker["worker"]["cwd"]
    hp_env, speaker_env = hp["worker"]["environment"], speaker["worker"]["environment"]
    assert {key: value for key, value in hp_env.items() if key != "WINEDLLOVERRIDES"} == {key: value for key, value in speaker_env.items() if key != "WINEDLLOVERRIDES"}
    assert speaker_env["WINEDLLOVERRIDES"] == hp_env["WINEDLLOVERRIDES"] + ";audioeng=n"
    assert "binaural stereo" in hp["scope"]
    assert speaker["scope"] == assets.SPEAKER_SCOPE
    for item, engine, blob in [(hp, "hrtf-headphones", "headphone-game-output-spatial-rendering.bin"),
                               (speaker, "hrtf-speakers", "speaker-game-supported-cp.bin")]:
        command = item["worker"]["command"]
        assert command[command.index("--engine") + 1] == engine
        assert command[command.index("--runtime-blob") + 1].endswith("\\original\\" + blob)
    differences = [index for index, (a, b) in enumerate(zip(hp["worker"]["command"], speaker["worker"]["command"])) if a != b]
    assert differences == [hp["worker"]["command"].index("--engine") + 1,
                           hp["worker"]["command"].index("--runtime-blob") + 1]
    assert len(speaker["worker"]["command"]) == len(hp["worker"]["command"]) + 8
    for option in ("--speaker-post-adapter", "--speaker-post-dll", "--speaker-post-plan", "--speaker-post-shim"):
        assert option not in hp["worker"]["command"]
        assert option in speaker["worker"]["command"]
