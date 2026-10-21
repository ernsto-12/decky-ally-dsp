"""Vendor-free readiness regression tests; no PipeWire process or audio access."""
import copy
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


def expected_routes():
    nodes = {"sink6": 10, "bridge.capture": 11, "bridge.playback": 12, "sink2": 13}
    routes = []
    for index, channel in enumerate(["FL", "FR", "FC", "LFE", "RL", "RR", "FL", "FR"]):
        output, input_ = ("sink6", "bridge.capture") if index < 6 else ("bridge.playback", "sink2")
        route = {"channel": channel}
        for offset, (side, name) in enumerate([("output", output), ("input", input_)]):
            route.update({side + "_node": name, side + "_node_id": nodes[name],
                          side + "_node_serial": 1000 + nodes[name],
                          side + "_port": 100 + index * 2 + offset,
                          side + "_port_serial": 2000 + index * 2 + offset,
                          side + "_port_name": side + "_" + channel,
                          side + "_channel": channel})
        routes.append(route)
    return routes


def snapshot(count=8, state="active", error=None, foreign=False):
    plan = expected_routes()
    links = []
    for index in range(count):
        link = dict(plan[index % len(plan)], id=500 + index, state=state, error=error)
        link.pop("channel")
        for side in ("output", "input"):
            link[side + "_port_node_id"] = link[side + "_node_id"]
            link[side + "_direction"] = side
        links.append(link)
    if foreign:
        links[0]["output_node"] = "unrequested-source"
    return {"links": links}


def wait(bridge, monkeypatch, graph, *, signalled=False, plan=None):
    clock = Clock()
    monkeypatch.setattr(bridge, "time", SimpleNamespace(monotonic=clock.now))
    observations = []
    result = bridge.wait_for_explicit_routes(
        graph, [], "sink6", "bridge.capture", "bridge.playback", "sink2", expected_routes() if plan is None else plan,
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
    assert observations[-1]["verified_channel_count"] == 8
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


def test_reversed_six_channel_input_fails_even_with_eight_active_links(bridge, monkeypatch):
    invalid = snapshot()
    previous = copy.deepcopy(invalid["links"][:6])
    for index in range(6):
        for key, value in previous[5 - index].items():
            if key.startswith("input_"):
                invalid["links"][index][key] = value
    graph = Graph([invalid])
    with pytest.raises(RuntimeError, match="identity or channel mismatch"):
        wait(bridge, monkeypatch, graph)
    assert graph.calls == 1


@pytest.mark.parametrize("side", ["output", "input"])
@pytest.mark.parametrize("suffix,value", [
    ("_node_id", 999), ("_node_serial", 999999), ("_port", 999),
    ("_port_serial", 999999), ("_port_name", "replacement_port"), ("_channel", "FR"),
], ids=["replacement-node", "recycled-node-id", "replacement-port", "recycled-port-id", "renamed-port", "wrong-channel"])
def test_endpoint_replacement_or_reuse_never_counts_as_ready(bridge, monkeypatch, side, suffix, value):
    invalid = snapshot()
    invalid["links"][0][side + suffix] = value
    graph = Graph([invalid])
    with pytest.raises(RuntimeError, match="identity or channel mismatch"):
        wait(bridge, monkeypatch, graph)
    assert graph.calls == 1


@pytest.mark.parametrize("key,value", [
    ("output_port_node_id", 999), ("input_port_node_id", 999),
    ("output_direction", "input"), ("input_direction", "output"),
], ids=["foreign-output-owner", "foreign-input-owner", "reversed-output-direction", "reversed-input-direction"])
def test_matching_identity_with_wrong_port_owner_or_direction_is_rejected(bridge, monkeypatch, key, value):
    invalid = snapshot()
    invalid["links"][0][key] = value
    graph = Graph([invalid])
    with pytest.raises(RuntimeError, match="ownership or direction mismatch"):
        wait(bridge, monkeypatch, graph)
    assert graph.calls == 1


def test_duplicate_channel_cannot_replace_a_missing_channel_at_same_link_count(bridge, monkeypatch):
    invalid = snapshot()
    invalid["links"][1] = dict(invalid["links"][0], id=999)
    graph = Graph([invalid])
    with pytest.raises(RuntimeError, match="duplicate channel links"):
        wait(bridge, monkeypatch, graph)
    assert graph.calls == 1


@pytest.mark.parametrize("missing", ["output_port_serial", "input_node_serial", "input_channel", "output_port_name"])
def test_late_endpoint_metadata_waits_for_verified_publication(bridge, monkeypatch, missing):
    partial = snapshot()
    del partial["links"][0][missing]
    complete = snapshot()
    graph = Graph([partial, complete])
    result, observations, clock = wait(bridge, monkeypatch, graph)
    assert result == complete
    assert graph.calls == 2
    assert observations[0]["owned_link_count"] == 8
    assert observations[0]["verified_channel_count"] == 7
    assert observations[0]["pending"] is True
    assert observations[1]["verified_channel_count"] == 8
    assert 0 < clock.value < 5


def test_permanently_missing_identity_metadata_has_a_bounded_deadline(bridge, monkeypatch):
    partial = snapshot()
    partial["links"][0]["output_port_serial"] = None
    graph = Graph([partial])
    with pytest.raises(TimeoutError, match="8/8 links"):
        wait(bridge, monkeypatch, graph)
    assert graph.calls <= 102


@pytest.mark.parametrize("invalid_plan", ["empty", "duplicate-channel", "foreign-pair", "missing-identity"])
def test_invalid_route_plan_is_rejected_before_inspecting_the_graph(bridge, monkeypatch, invalid_plan):
    plan = expected_routes()
    if invalid_plan == "empty":
        plan = []
    elif invalid_plan == "duplicate-channel":
        plan[1] = dict(plan[0])
    elif invalid_plan == "foreign-pair":
        plan[0]["output_node"] = "another-sink"
    else:
        del plan[0]["input_port_serial"]
    graph = Graph([snapshot()])
    with pytest.raises(ValueError):
        wait(bridge, monkeypatch, graph, plan=plan)
    assert graph.calls == 0


def registry_dump():
    objects = {}
    for index, route in enumerate(expected_routes()):
        info = {"state": "active", "error": None}
        for side in ("output", "input"):
            node_id, port_id = route[side + "_node_id"], route[side + "_port"]
            objects[node_id] = {"id": node_id, "type": "PipeWire:Interface:Node", "info": {"props": {
                "node.name": route[side + "_node"], "object.serial": route[side + "_node_serial"]}}}
            objects[port_id] = {"id": port_id, "type": "PipeWire:Interface:Port", "info": {
                "direction": side, "props": {"node.id": node_id, "port.name": route[side + "_port_name"],
                "audio.channel": route[side + "_channel"], "object.serial": route[side + "_port_serial"]}}}
            info[side + "-node-id"], info[side + "-port-id"] = node_id, port_id
        objects[500 + index] = {"id": 500 + index, "type": "PipeWire:Interface:Link", "info": info}
    return list(objects.values())


@pytest.mark.parametrize("changed_channel", [False, True], ids=["valid-registry", "wrong-registry-channel"])
def test_real_audit_reads_actual_port_channels_before_ready(bridge, monkeypatch, changed_channel):
    graph = bridge.Graph("fixture-only")
    raw = registry_dump()
    if changed_channel:
        next(item for item in raw if item["id"] == 101)["info"]["props"]["audio.channel"] = "RR"
    monkeypatch.setattr(graph, "dump", lambda: raw)
    if changed_channel:
        with pytest.raises(RuntimeError, match="identity or channel mismatch"):
            wait(bridge, monkeypatch, graph)
    else:
        result, observations, _ = wait(bridge, monkeypatch, graph)
        assert len(result["links"]) == 8
        assert observations[-1]["verified_channel_count"] == 8
