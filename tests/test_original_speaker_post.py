"""Vendor-free speaker composition and fail-closed asset checks."""
import hashlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def worker(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "py_modules/allydsp/original"
    monkeypatch.syspath_prepend(str(source))
    spec = importlib.util.spec_from_file_location("_speaker_post_worker_test", source / "windows_worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def settings(profile="hrtf-speakers", **paths):
    return SimpleNamespace(engine=profile, speaker_post_adapter=paths.get("adapter"),
                           speaker_post_dll=paths.get("dll"), speaker_post_plan=paths.get("plan"),
                           speaker_post_shim=paths.get("shim"))


@pytest.fixture
def post_paths(tmp_path, worker, monkeypatch):
    paths = {key: tmp_path / name for key, name in (
        ("adapter", "adapter.py"), ("dll", "speaker.dll"), ("plan", "plan.json"), ("shim", "audioeng.dll"))}
    pins = {}
    for key, path in paths.items():
        path.write_bytes(("generated " + key).encode())
        pins[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(worker, "POST_PINS", pins)
    return paths


def test_speaker_requires_every_post_asset_before_loading_any_dll(worker, post_paths):
    assert worker._speaker_post_paths(settings(**post_paths)) == post_paths
    for key in post_paths:
        incomplete = dict(post_paths, **{key: None})
        with pytest.raises(ValueError, match="requires all verified"):
            worker.create_engine(settings(**incomplete))


@pytest.mark.parametrize("profile", ["hrtf-headphones", "vr-game"])
def test_other_routes_cannot_apply_speaker_post_stage(worker, profile, post_paths):
    assert worker._speaker_post_paths(settings(profile)) is None
    with pytest.raises(ValueError, match="forbidden"):
        worker.create_engine(settings(profile, **post_paths))


@pytest.mark.parametrize("key", ["adapter", "dll", "plan", "shim"])
@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_missing_or_changed_post_asset_fails_before_original_initialization(worker, post_paths, key, damage):
    path = post_paths[key]
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"different from the selected reference")
    with pytest.raises(ValueError, match="asset pin differs"):
        worker.create_engine(settings(**post_paths))


def test_shim_cannot_be_loaded_from_an_unrelated_directory(worker, post_paths, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    moved = other / "audioeng.dll"
    moved.write_bytes(post_paths["shim"].read_bytes())
    post_paths["shim"] = moved
    with pytest.raises(ValueError, match="must be adjacent"):
        worker.create_engine(settings(**post_paths))


def test_post_startup_failure_closes_the_initialized_frontend(worker, post_paths, monkeypatch):
    events = []
    frontend = SimpleNamespace(close=lambda: events.append("close-frontend"))
    monkeypatch.setitem(sys.modules, "hrtf_adapter", SimpleNamespace(HrtfStreamAdapter=lambda **kwargs: frontend))

    def fail_shim(path):
        raise OSError("allocator unavailable")

    monkeypatch.setattr(worker.ctypes, "WinDLL", fail_shim, raising=False)
    namespace = settings(**post_paths)
    namespace.__dict__.update(frontend_adapter="adapter", hrtf="hrtf", init_blob="init",
                              initial_runtime_blob="initial", runtime_blob="runtime", dll="dll",
                              property_store="store", input_channels=6)
    with pytest.raises(OSError, match="allocator unavailable"):
        worker.create_engine(namespace)
    assert events == ["close-frontend"]


def test_composition_keeps_original_frontend_before_speaker_post_and_reports_both(worker):
    events = []

    class Frontend:
        block_frames, sample_rate, input_channels, output_channels = 256, 48000, 6, 2
        metadata = {"engine": "original-frontend", "profile": "hrtf-speakers"}

        def process(self, payload):
            assert payload == b"six-channel input"
            events.append("frontend")
            return b"stereo intermediate"

        def close(self):
            events.append("close-frontend")
            return {"processed_blocks": 1, "same_dsp_instance": True, "guards_unchanged": True}

    class Post:
        block_frames, sample_rate, input_channels, output_channels = 256, 48000, 2, 2
        metadata = {"engine": "original-vlldp160-candidate-post", "declared_latency_frames": 176}

        def process(self, payload):
            assert payload == b"stereo intermediate"
            events.append("post")
            return b"final stereo"

        def close(self):
            events.append("close-post")
            return {"processed_blocks": 1, "same_dsp_instance": True, "guards_unchanged": True}

    stream = worker.SpeakerPostStream(Frontend(), Post(), object())
    assert stream.input_channels == 6 and stream.output_channels == 2
    assert stream.metadata["additional_post_latency_frames"] == 176
    assert stream.process(b"six-channel input") == b"final stereo"
    summary = stream.close()
    assert events == ["frontend", "post", "close-post", "close-frontend"]
    assert summary["guards_unchanged"] and summary["same_dsp_instance"]
    assert summary["speaker_post"]["processed_blocks"] == summary["processed_blocks"] == 1
    with pytest.raises(RuntimeError, match="closed"):
        stream.process(b"six-channel input")
