"""Startup receipt checks with generated assets; no Dolby code or tuning."""
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def host(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "py_modules/allydsp/original/hrtf_adapter.py"
    spec = importlib.util.spec_from_file_location("_original_hrtf_startup_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The production adapter temporarily adds its verified module directory.
    monkeypatch.syspath_prepend(str(source.parent))
    return module


def notification(pin):
    return {"blob_sha256": pin, "same_module": True, "same_dsp": True,
            "callback_queries": [{"kind": "blob_bytes", "supplied": True, "sha256": pin}]}


@pytest.mark.parametrize("change", [
    {"blob_sha256": "b" * 64}, {"same_module": False}, {"same_dsp": False},
    {"callback_queries": []}, {"callback_queries": [{"kind": "blob_bytes", "supplied": False, "sha256": "a" * 64}]},
    {"callback_queries": [{"kind": "blob_bytes", "supplied": True, "sha256": "b" * 64}]},
])
def test_runtime_delivery_rejects_other_blob_or_dsp(host, change):
    receipt = dict(notification("a" * 64), **change)
    with pytest.raises(RuntimeError):
        host._verify_runtime_notification(receipt, "a" * 64)


@pytest.mark.parametrize("profile,mode", [("hrtf-headphones", 0), ("hrtf-speakers", 1)])
def test_stream_checks_delivery_before_ready_and_reports_readback(host, tmp_path, monkeypatch, profile, mode):
    arguments = {}
    pins = {}
    for key in ("adapter", "dll", "hrtf", "init_blob", "initial_runtime_blob", "runtime_blob", "property_store"):
        name = "local_property_store.py" if key == "property_store" else key + ".fixture"
        path = tmp_path / name
        path.write_bytes(("generated dummy asset " + key).encode())
        arguments[key] = path
        pins[profile if key == "runtime_blob" else key] = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(host, "PINS", pins)
    state = {"sample_rate": 48000, "module_initialized": 1}
    calls = []
    state_bytes = b"test" * 12
    pins["speaker_level_state"] = hashlib.sha256(state_bytes).hexdigest()

    class Delegate:
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)
            self.metadata = {"runtime_notification": notification(pins[profile])}
            self.engine = SimpleNamespace(state_readback=lambda: dict(state), dsp=0x1000)

    fake_module = SimpleNamespace(PcmFrontendAdapter=Delegate,
                                  C=SimpleNamespace(string_at=lambda address, size: b"test"))
    monkeypatch.setattr(host.importlib.util, "spec_from_file_location",
                        lambda *args: SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None)))
    monkeypatch.setattr(host.importlib.util, "module_from_spec", lambda spec: fake_module)
    stream = host.HrtfStreamAdapter(profile=profile, input_channels=6, **arguments)
    assert calls[0]["mode"] == mode
    assert stream.metadata["runtime_notification_verified"] is True
    assert stream.metadata["configuration_readback"] == state
    assert stream.metadata["configuration_readback_phase"] == "after-notification-before-first-process"
    assert stream.metadata["runtime_blob_sha256"] == pins[profile]
    assert stream.metadata["speaker_level_state_verified"] == (profile == "hrtf-speakers")
    assert stream.metadata["speaker_level_state_sha256"] == (pins["speaker_level_state"] if mode else None)

    if mode:
        fake_module.C.string_at = lambda address, size: b"oops"
        with pytest.raises(RuntimeError, match="level-control readback differs"):
            host.HrtfStreamAdapter(profile=profile, input_channels=6, **arguments)
        fake_module.C.string_at = lambda address, size: b"test"

    class WrongEngine(Delegate):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.metadata["runtime_notification"]["same_dsp"] = False

    fake_module.PcmFrontendAdapter = WrongEngine
    with pytest.raises(RuntimeError, match="retain the verified engine"):
        host.HrtfStreamAdapter(profile=profile, input_channels=6, **arguments)
