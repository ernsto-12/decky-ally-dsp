"""Resume recovery must distinguish a live routed engine from stale READY."""
import copy
import json

import pytest

from allydsp import original_runtime as original
from allydsp.util import Result


PHYSICAL = "alsa_output.pci-0000_64_00.6.analog-stereo"


def graph():
    names = [original.CAPTURE_SINK, original.NAMESPACE + ".capture",
             original.NAMESPACE + ".playback", PHYSICAL]
    dump = []
    for index, name in enumerate(names, 10):
        props = {"node.name": name, "object.serial": 1000 + index}
        if name == PHYSICAL:
            props["media.class"] = "Audio/Sink"
        if name == original.CAPTURE_SINK:
            props["allydsp.session"] = "owned-session"
        dump.append({"id": index, "type": "PipeWire:Interface:Node", "info": {"props": props}})
    for index, channel in enumerate(original.POSITIONS + ["FL", "FR"]):
        output, input_ = (10, 11) if index < 6 else (12, 13)
        for offset, (direction, node) in enumerate((("output", output), ("input", input_))):
            port = 100 + 2 * index + offset
            dump.append({"id": port, "type": "PipeWire:Interface:Port",
                         "info": {"direction": direction, "props": {
                             "node.id": node, "audio.channel": channel, "object.serial": 2000 + port}}})
        dump.append({"id": 300 + index, "type": "PipeWire:Interface:Link", "info": {
            "state": "active", "error": None, "output-node-id": output, "input-node-id": input_,
            "output-port-id": 100 + 2 * index, "input-port-id": 101 + 2 * index}})
    return dump


@pytest.fixture
def probe(monkeypatch):
    class Probe:
        dump = graph()
        journal = {"schema": "ally-dsp-original-routing-v2", "routed": True, "restored": False,
                   "token": "owned-session", "physical_sink": PHYSICAL,
                   "master_mirror": {"schema": "ally-dsp-original-physical-master-v1",
                                     "physical_identity": {"name": PHYSICAL, "serial": "1013", "index": 77}}}
        state = {"ready": True, "state": "ready", "daemon_pid": 42, "node_routes_verified": True,
                 "capture_sink": original.CAPTURE_SINK, "playback_sink": PHYSICAL,
                 "updated_unix_seconds": 1000.0, "last_error": None}
        default = original.CAPTURE_SINK
        service = "ActiveState=active\nMainPID=42\n"
        commands = []

        def run(self, argv, *, timeout):
            self.commands.append((argv, timeout))
            if argv == ["pw-dump"]:
                return Result(0, json.dumps(self.dump), "")
            if argv == ["pactl", "get-default-sink"]:
                return Result(0, self.default + "\n", "")
            raise AssertionError("Unexpected audio mutation: " + repr(argv))

        def systemctl(self, *args, timeout):
            self.commands.append((list(args), timeout))
            assert args == ("show", "--property=ActiveState", "--property=MainPID", original.paths.UNIT_NAME)
            return Result(0, self.service, "")

        def read(self, path, default=None):
            assert path in (original.STATUS_FILE, original.ROUTING_FILE)
            return self.state if path == original.STATUS_FILE else self.journal

    p = Probe()
    monkeypatch.setattr(original, "run", p.run)
    monkeypatch.setattr(original.converted_runtime, "systemctl", p.systemctl)
    monkeypatch.setattr(original, "read_json", p.read)
    monkeypatch.setattr(original.time, "time", lambda: 1000.0)
    return p


def test_healthy_routes_are_current_and_checks_do_not_modify_audio_or_journals(probe):
    before = copy.deepcopy((probe.dump, probe.state, probe.journal))
    assert original.health() == {"healthy": True, "available": True, "reason": "ready",
                                 "links": {"input": 6, "output": 2}}
    assert before == (probe.dump, probe.state, probe.journal)
    assert len(probe.commands) == 3
    assert all(timeout == 3 for _, timeout in probe.commands)


@pytest.mark.parametrize("stamp", [None, True, 991, 1003, float("nan"), float("inf")])
def test_matching_main_pid_with_old_or_invalid_heartbeat_is_not_healthy(probe, stamp):
    probe.state["updated_unix_seconds"] = stamp
    assert original.health()["reason"] == "stale-worker-heartbeat"


@pytest.mark.parametrize("service,reason", [
    ("ActiveState=failed\nMainPID=0\n", "service-inactive"),
    ("ActiveState=active\nMainPID=99\n", "daemon-identity-mismatch"),
])
def test_old_ready_does_not_hide_failed_or_replaced_daemon(probe, service, reason):
    probe.service = service
    assert original.health() == {"healthy": False, "available": True, "reason": reason,
                                 "links": {"input": 0, "output": 0}}


def test_default_bypass_is_recoverable_but_explicit_external_output_is_preserved(probe):
    probe.default = PHYSICAL
    assert original.health()["reason"] == "default-bypasses-original"
    probe.default = "bluez_output.user_headphones"
    assert original.health()["available"] is False
    assert original.health()["reason"] == "external-output-selected"


@pytest.mark.parametrize("missing,reason", [
    ("all", "pipewire-unavailable"), (PHYSICAL, "physical-output-unavailable"),
])
def test_waits_for_pipewire_and_analog_output_to_reappear(probe, missing, reason):
    probe.dump = [] if missing == "all" else [
        item for item in probe.dump if item.get("info", {}).get("props", {}).get("node.name") != missing]
    assert original.health()["available"] is False
    assert original.health()["reason"] == reason


def test_missing_capture_sink_is_recoverable_but_replaced_sink_is_not_owned(probe):
    sink = probe.dump[0]
    probe.dump.remove(sink)
    assert original.health()["reason"] == "capture-sink-unavailable"
    assert original.health()["available"] is True
    probe.dump.insert(0, sink)
    sink["info"]["props"]["allydsp.session"] = "external-session"
    assert original.health()["reason"] == "capture-sink-ownership-changed"
    assert original.health()["available"] is False


@pytest.mark.parametrize("state", ["error", "unlinked", "negotiating", "init"])
def test_eight_links_are_insufficient_when_a_link_has_not_activated(probe, state):
    link = next(x for x in probe.dump if x["type"] == "PipeWire:Interface:Link")
    link["info"]["state"] = state
    assert original.health()["reason"] == "inactive-or-failed-link"


def test_paused_links_are_valid_and_do_not_trigger_restarts_when_idle(probe):
    for link in probe.dump:
        if link["type"] == "PipeWire:Interface:Link":
            link["info"]["state"] = "paused"
    assert original.health()["healthy"] is True


def test_crosswired_channels_fail_despite_exact_eight_link_count(probe):
    port = next(x for x in probe.dump if x.get("id") == 101)
    port["info"]["props"]["audio.channel"] = "FR"
    assert original.health()["reason"] == "unexpected-channel-route"


def test_duplicate_channel_fails_even_when_one_other_link_is_removed(probe):
    links = [x for x in probe.dump if x["type"] == "PipeWire:Interface:Link"]
    links[1]["info"] = copy.deepcopy(links[0]["info"])
    assert original.health()["reason"] == "duplicate-channel-route"


def test_missing_link_fails_while_service_and_ready_remain_true(probe):
    probe.dump.remove(next(x for x in probe.dump if x["type"] == "PipeWire:Interface:Link"))
    assert original.health()["reason"] == "incomplete-channel-routes"


@pytest.mark.parametrize("change", ["owner", "direction", "serial"])
def test_current_port_ownership_and_identity_must_match_link(probe, change):
    info = next(x for x in probe.dump if x.get("id") == 101)["info"]
    if change == "owner":
        info["props"]["node.id"] = 999
    elif change == "direction":
        info["direction"] = "output"
    else:
        info["props"]["object.serial"] = None
    assert original.health()["reason"] == "invalid-port-identity"


def test_status_cannot_report_verified_from_stale_ready_with_a_missing_route(probe, monkeypatch):
    probe.dump.remove(next(x for x in probe.dump if x["type"] == "PipeWire:Interface:Link"))
    monkeypatch.setattr(original, "verify", lambda *_: True)
    monkeypatch.setattr(original, "active_meta", lambda: None)
    monkeypatch.setattr(original, "unit_installed", lambda: True)
    monkeypatch.setattr(original.converted_runtime, "is_active", lambda: True)
    monkeypatch.setattr(original.converted_runtime, "is_enabled", lambda: True)
    status = original.status(with_journal=False)
    assert status["active"] is True
    assert status["original"]["ready"] is True
    assert status["verified"] is False
    assert status["health"]["reason"] == "incomplete-channel-routes"


@pytest.mark.parametrize("result", [Result(124, "", "timeout"), Result(0, "not json", "")])
def test_graph_timeout_or_partial_output_defers_recovery_until_session_returns(probe, monkeypatch, result):
    monkeypatch.setattr(original, "run", lambda *args, **kwargs: result)
    assert original.health()["reason"] == "pipewire-unavailable"
    assert original.health()["available"] is False


def test_same_named_physical_output_with_new_serial_requires_rebuilding_routes(probe):
    physical = next(x for x in probe.dump if x.get("id") == 13)
    physical["info"]["props"]["object.serial"] = 9999
    state = original.health()
    assert state["healthy"] is False
    assert state["available"] is True
    assert state["reason"] == "physical-output-recreated"


def test_physical_identity_matches_serial_without_comparing_pulse_index_to_pipewire_id(probe):
    assert probe.journal["master_mirror"]["physical_identity"]["index"] != 13
    assert original.health()["healthy"] is True


@pytest.mark.parametrize("identity", [None, {}, {"name": PHYSICAL},
                                     {"name": "another-output", "serial": 1013},
                                     {"name": PHYSICAL, "serial": True}])
def test_missing_or_malformed_journal_identity_does_not_authorize_automatic_rebuild(probe, identity):
    probe.journal["master_mirror"]["physical_identity"] = identity
    state = original.health()
    assert state["reason"] == "routing-identity-unavailable"
    assert state["available"] is False


def test_legacy_volume_handoff_uses_its_explicit_identity_and_never_guesses_an_absent_one(probe):
    probe.journal["schema"] = "ally-dsp-original-routing-v1"
    old_owner = probe.journal.pop("master_mirror")
    old_owner["schema"] = "ally-dsp-original-volume-handoff-v1"
    probe.journal["volume_handoff"] = old_owner
    assert original.health()["healthy"] is True
    del probe.journal["volume_handoff"]
    assert original.health()["available"] is False
    assert original.health()["reason"] == "routing-identity-unavailable"


def test_unpublished_physical_serial_waits_for_complete_pipewire_registry(probe):
    physical = next(x for x in probe.dump if x.get("id") == 13)
    del physical["info"]["props"]["object.serial"]
    assert original.health()["available"] is False
    assert original.health()["reason"] == "physical-identity-unavailable"


@pytest.mark.parametrize("change", ["journal", "owner"])
def test_unknown_ownership_schema_defers_automatic_mutation(probe, change):
    if change == "journal":
        probe.journal["schema"] = "unknown-routing-schema"
    else:
        probe.journal["master_mirror"]["schema"] = "ally-dsp-original-volume-handoff-v1"
    assert original.health()["available"] is False
    assert original.health()["reason"] == "routing-identity-unavailable"
