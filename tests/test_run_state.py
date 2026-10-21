import asyncio

from allydsp import jackwatch, settings, setup_flow


class Device:
    def __init__(self, headphones, active):
        self.headphones, self.active, self.calls = headphones, active, []

    def stop(self):
        self.calls.append("stop")
        self.active = False

    def start(self):
        self.calls.append("start")
        self.active = True


def _device(monkeypatch, headphones, active):
    dev = Device(headphones, active)
    # These tests exercise the converted chain. Do not read the real device's
    # selected engine from its settings when running the suite on a handheld.
    monkeypatch.setattr(jackwatch.dsp_runtime, "pause_for_headphones", lambda: True)
    monkeypatch.setattr(jackwatch.hardware, "pw_dump", lambda: [{}])
    monkeypatch.setattr(jackwatch.hardware, "output_route", lambda dump: None)
    monkeypatch.setattr(jackwatch.hardware, "headphones_active", lambda route: dev.headphones)
    monkeypatch.setattr(jackwatch.dsp_runtime, "is_active", lambda: dev.active)
    monkeypatch.setattr(jackwatch.dsp_runtime, "stop", dev.stop)
    monkeypatch.setattr(jackwatch.dsp_runtime, "start", dev.start)
    return dev


def test_headphones_pause_and_resume_the_chain(monkeypatch):
    dev = _device(monkeypatch, headphones=False, active=True)
    w = jackwatch.JackWatcher()
    asyncio.run(w.poll(lambda: True))
    dev.headphones = True
    asyncio.run(w.poll(lambda: True))
    assert dev.calls == ["stop"] and w.paused
    dev.headphones = False
    asyncio.run(w.poll(lambda: True))
    assert dev.calls == ["stop", "start"] and not w.paused


def test_chain_switched_on_during_headphone_use_starts_on_unplug(monkeypatch):
    dev = _device(monkeypatch, headphones=True, active=False)
    w = jackwatch.JackWatcher()
    asyncio.run(w.poll(lambda: False))
    assert w.paused and dev.calls == []
    dev.headphones = False
    asyncio.run(w.poll(lambda: True))
    assert dev.calls == ["start"]


def test_unplug_leaves_a_switched_off_chain_alone(monkeypatch):
    dev = _device(monkeypatch, headphones=True, active=False)
    w = jackwatch.JackWatcher()
    asyncio.run(w.poll(lambda: False))
    dev.headphones = False
    asyncio.run(w.poll(lambda: False))
    assert dev.calls == []


def _activate(monkeypatch, enabled, headphones):
    calls = []
    monkeypatch.setattr(setup_flow.hardware, "pw_dump", lambda: [])
    monkeypatch.setattr(setup_flow.hardware, "output_route", lambda dump: None)
    monkeypatch.setattr(setup_flow.hardware, "headphones_active", lambda route: headphones)
    monkeypatch.setattr(setup_flow.dsp_runtime, "apply_preset",
                        lambda profile, voicing, pregain, restart: calls.append(("apply", restart)) or {"verified": True})
    monkeypatch.setattr(setup_flow.dsp_runtime, "enable", lambda on: calls.append(("enable", on)))
    monkeypatch.setattr(setup_flow.dsp_runtime, "stop", lambda: calls.append(("stop",)))
    events = []
    setup_flow._activate(events.append, settings._merge(settings.DEFAULTS, {"enabled": enabled}), 0.0)
    return calls, events[-1]["message"]


def test_setup_starts_the_chain_only_when_switched_on_and_no_headphones(monkeypatch):
    calls, msg = _activate(monkeypatch, enabled=True, headphones=False)
    assert calls == [("apply", True), ("enable", True)] and msg.startswith("Active")
    calls, msg = _activate(monkeypatch, enabled=False, headphones=False)
    assert calls == [("apply", False), ("enable", False), ("stop",)] and "DSP is off" in msg
    calls, msg = _activate(monkeypatch, enabled=True, headphones=True)
    assert calls == [("apply", False), ("enable", True), ("stop",)] and "headphones" in msg
