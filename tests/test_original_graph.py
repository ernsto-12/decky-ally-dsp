"""Vendor-free readiness regression tests; no PipeWire process or audio access."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def bridge(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "py_modules/allydsp/original"
    monkeypatch.syspath_prepend(str(source))
    spec = importlib.util.spec_from_file_location("_allydsp_graph_readiness_test", source / "pipewire_bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    value = 0.0

    def now(self):
        return self.value


class Stop:
    def __init__(self, clock, requested=False):
        self.clock = clock
        self.requested = requested

    def is_set(self):
        return self.requested

    def wait(self, seconds):
        self.clock.value += seconds
        return self.requested


class Graph:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.calls = 0

    def audit(self, allowed):
        self.calls += 1
        return self.snapshots[min(self.calls - 1, len(self.snapshots) - 1)]


def snapshot(count=8, state="active", error=None, foreign=False):
    links = []
    for index in range(count):
        output, input_ = ("sink6", "bridge.capture") if index < 6 else ("bridge.playback", "sink2")
        links.append({"output_node": output, "input_node": input_, "state": state, "error": error})
    if foreign:
        links[0]["output_node"] = "unrequested-source"
    return {"links": links}


def wait(bridge, monkeypatch, graph, *, signalled=False):
    clock = Clock()
    monkeypatch.setattr(bridge, "time", SimpleNamespace(monotonic=clock.now))
    observations = []
    result = bridge.wait_for_explicit_routes(
        graph, [], "sink6", "bridge.capture", "bridge.playback", "sink2", 8,
        Stop(clock, signalled), {"signal": "SIGTERM" if signalled else None}, observations)
    return result, observations, clock


@pytest.mark.parametrize("snapshots", [
    [snapshot(7), snapshot(8)],
    [snapshot(8, "negotiating"), snapshot(8)],
    [snapshot(8, "paused")],
], ids=["delayed-eighth-link", "negotiating-then-active", "paused-valid"])
def test_ready_waits_for_exact_complete_routes(bridge, monkeypatch, snapshots):
    graph = Graph(snapshots)
    result, observations, clock = wait(bridge, monkeypatch, graph)
    assert result == snapshots[-1]
    assert graph.calls == len(snapshots)
    assert observations[-1]["owned_link_count"] == 8
    assert observations[-1]["pending"] is False
    assert clock.value < 5


@pytest.mark.parametrize("invalid", [
    snapshot(8, foreign=True), snapshot(9), snapshot(8, error="negotiation failed"),
], ids=["unexpected-owned-link", "duplicate-owned-link", "permanent-link-error"])
def test_unsafe_routes_fail_without_retry(bridge, monkeypatch, invalid):
    graph = Graph([invalid])
    with pytest.raises(RuntimeError):
        wait(bridge, monkeypatch, graph)
    assert graph.calls == 1


def test_permanently_missing_route_has_a_bounded_deadline(bridge, monkeypatch):
    graph = Graph([snapshot(7)])
    with pytest.raises(TimeoutError, match="7/8 links"):
        wait(bridge, monkeypatch, graph)
    assert graph.calls <= 102


def test_stop_signal_cancels_startup_without_publishing_ready(bridge, monkeypatch):
    graph = Graph([snapshot(7)])
    result, observations, _ = wait(bridge, monkeypatch, graph, signalled=True)
    assert result is None
    assert observations == []
    assert graph.calls == 0
