"""Failure/recovery semantics with an in-memory Pulse session and shared service.

These tests never invoke pactl/systemd or change the host audio session. They
also run with unittest so the offline Windows research runtime needs no pytest.
"""
import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import shutil
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from contextlib import ExitStack

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py_modules"))
from allydsp import original_runtime as original, dsp_runtime
from allydsp.util import Result, read_json, write_json


class AudioSession:
    def __init__(self):
        self.sinks = [
            {"name": "speaker", "index": 10, "owner_module": 1, "properties": {}},
            {"name": "usb", "index": 20, "owner_module": 2, "properties": {}},
        ]
        self.streams = [{"index": 100, "sink": 10, "properties": {
            "application.process.id": "123", "media.name": "Game", "object.serial": "1000"}}]
        self.default = "speaker"
        self.calls = []
        self.fail_move = set()
        self.disappear_on_move = set()

    def owned(self, token="session", module=42):
        self.sinks.append({"name": original.CAPTURE_SINK, "index": 99,
                           "owner_module": module, "properties": {"allydsp.session": token}})

    def run(self, args, **kwargs):
        self.calls.append(tuple(args))
        assert args[0] == "pactl", "Unexpected real command requested"
        if args[1:4] == ["-f", "json", "list"]:
            return Result(0, json.dumps(self.sinks if args[4] == "sinks" else self.streams), "")
        op = args[1]
        if op == "get-default-sink":
            return Result(0, self.default + "\n", "")
        if op == "set-default-sink":
            self.default = args[2]
        elif op == "move-sink-input":
            index = int(args[2])
            if index in self.disappear_on_move:
                self.streams = [s for s in self.streams if s["index"] != index]
                return Result(1, "", "stream disappeared")
            if index in self.fail_move:
                return Result(1, "", "move failed")
            target = next(s for s in self.sinks if s["name"] == args[3])
            next(s for s in self.streams if s["index"] == index)["sink"] = target["index"]
        elif op == "load-module":
            # The recovery journal must already exist before a module can be created.
            journal = read_json(original.ROUTING_FILE)
            assert journal and journal["token"] and journal["module_id"] is None
            assert "allydsp.session=" + journal["token"] in args[-1]
            self.owned(journal["token"])
            return Result(0, "42\n", "")
        elif op == "unload-module":
            self.sinks = [s for s in self.sinks if str(s["owner_module"]) != args[2]]
        else:
            raise AssertionError("Unexpected pactl command: " + repr(args))
        return Result(0, "", "")

    def mutations(self):
        return [x for x in self.calls if x[1] in
                ("set-default-sink", "move-sink-input", "load-module", "unload-module")]


class OriginalRoutingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="original-runtime-test-", dir=ROOT / "tests"))
        self.directory = Path(self.temp)
        for key, name in [("ORIGINAL_DIR", "original"), ("ROUTING_FILE", "routing.json"),
                          ("RUNTIME_FILE", "runtime.json"), ("STATUS_FILE", "status.json"),
                          ("META_FILE", "active.json")]:
            self.stack.enter_context(patch.object(original, key, str(self.directory / name)))
        self.audio = AudioSession()
        self.stack.enter_context(patch.object(original, "run", self.audio.run))
        self.stack.enter_context(patch.object(original.hardware, "pw_dump", lambda: []))
        self.stack.enter_context(patch.object(original.hardware, "find_speaker_sink",
                                             lambda _: next((s for s in self.audio.sinks if s["name"] == "speaker"), None)))

    def journal(self, moved=True):
        record = {"schema": "ally-dsp-original-routing-v1", "previous_default": "usb",
                  "physical_sink": "speaker", "module_id": 42, "token": "session",
                  "restored": False, "moved": []}
        if moved:
            record["moved"].append({"index": 100, "sink_name": "speaker",
                                    "identity": original._stream_identity(self.audio.streams[0])})
        write_json(original.ROUTING_FILE, record)
        return record

    def routed(self):
        self.journal()
        self.audio.owned()
        self.audio.default = original.CAPTURE_SINK
        self.audio.streams[0]["sink"] = 99

    def test_restore_recovers_default_and_each_original_stream_destination(self):
        self.routed()
        original._restore_routing_locked()
        self.assertEqual(self.audio.default, "usb")
        self.assertEqual(self.audio.streams[0]["sink"], 10)
        self.assertTrue(read_json(original.ROUTING_FILE)["restored"])
        self.assertFalse(any(s["name"] == original.CAPTURE_SINK for s in self.audio.sinks))
        mutations = len(self.audio.mutations())
        original._restore_routing_locked()
        self.assertEqual(len(self.audio.mutations()), mutations, "Recovery must be idempotent")

    def test_restore_preserves_the_users_new_default(self):
        self.routed()
        self.audio.default = "usb"
        original._restore_routing_locked()
        self.assertEqual(self.audio.default, "usb")
        self.assertEqual(self.audio.streams[0]["sink"], 20)
        self.assertFalse(any(c[1] == "set-default-sink" for c in self.audio.calls))

    def test_reused_stream_index_does_not_restore_an_old_apps_destination(self):
        self.routed()
        self.audio.streams[0]["properties"]["object.serial"] = "replacement"
        original._restore_routing_locked()
        self.assertEqual(self.audio.streams[0]["sink"], 20)

    def test_restore_rejects_either_foreign_module_or_foreign_token(self):
        for module, token in [(999, "session"), (42, "foreign")]:
            with self.subTest(module=module, token=token):
                self.audio = AudioSession()
                original.run = self.audio.run
                self.journal()
                self.audio.owned(token, module)
                with self.assertRaisesRegex(RuntimeError, "replaced"):
                    original._restore_routing_locked()
                self.assertEqual(self.audio.mutations(), [])
                self.assertFalse(read_json(original.ROUTING_FILE)["restored"])

    def test_missing_outputs_leave_recovery_pending(self):
        self.routed()
        self.audio.sinks = [s for s in self.audio.sinks if s["index"] == 99]
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            original._restore_routing_locked()
        self.assertFalse(read_json(original.ROUTING_FILE)["restored"])
        self.assertEqual(self.audio.mutations(), [])

    def test_move_failure_on_a_live_stream_keeps_owned_sink_for_retry(self):
        self.routed()
        self.audio.fail_move.add(100)
        with self.assertRaisesRegex(RuntimeError, "move failed"):
            original._restore_routing_locked()
        self.assertFalse(read_json(original.ROUTING_FILE)["restored"])
        self.assertTrue(any(s["index"] == 99 for s in self.audio.sinks))
        self.assertFalse(any(c[1] == "unload-module" for c in self.audio.calls))

    def test_disappearing_stream_is_not_a_recovery_failure(self):
        self.routed()
        self.audio.disappear_on_move.add(100)
        original._restore_routing_locked()
        self.assertTrue(read_json(original.ROUTING_FILE)["restored"])
        self.assertEqual(self.audio.streams, [])

    def test_prepare_journals_ownership_before_load_and_does_not_route_early(self):
        original._prepare_route_locked()
        journal = read_json(original.ROUTING_FILE)
        self.assertEqual(journal["module_id"], 42)
        self.assertTrue(journal["token"])
        self.assertEqual(self.audio.default, "speaker")
        self.assertEqual(self.audio.streams[0]["sink"], 10)
        self.assertEqual([x[1] for x in self.audio.mutations()], ["load-module"])

    def test_dead_worker_cannot_take_over_default_or_streams(self):
        original._prepare_route_locked()
        before = len(self.audio.mutations())
        with patch.object(original, "verify", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "exited"):
                original._route_after_ready_locked()
        self.assertEqual(len(self.audio.mutations()), before)
        self.assertEqual(self.audio.default, "speaker")

    def test_worker_own_playback_stream_is_not_routed_back_into_capture(self):
        original._prepare_route_locked()
        self.audio.streams.append({"index": 101, "sink": 10,
                                   "properties": {"node.name": original.NAMESPACE + ".playback"}})
        with patch.object(original, "verify", return_value=True):
            original._route_after_ready_locked()
        self.assertEqual(self.audio.streams[0]["sink"], 99)
        self.assertEqual(self.audio.streams[1]["sink"], 10)
        self.assertEqual(self.audio.default, original.CAPTURE_SINK)
        self.assertEqual(read_json(original.ROUTING_FILE)["moved"][0]["index"], 100)

    def test_startup_readiness_failure_restores_routes_and_stops_shared_service(self):
        calls = []
        def routing(action):
            calls.append(action)
            {"prepare": original._prepare_route_locked, "route": original._route_after_ready_locked,
             "restore": original._restore_routing_locked}[action]()
        with patch.object(original, "_configuration", return_value={"engine_profile": "speakers", "worker": {"command": ["worker"]}}), \
             patch.object(original, "_routing_action", side_effect=routing), \
             patch.object(original, "ensure_unit"), \
             patch.object(original, "verify", return_value=False), \
             patch.object(original.converted_runtime, "is_active", return_value=False), \
             patch.object(original.converted_runtime, "start", return_value=True), \
             patch.object(original.converted_runtime, "stop", side_effect=lambda: calls.append("stop") or True):
            with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                original.start()
        self.assertEqual(calls, ["stop", "prepare", "stop", "restore"])
        self.assertEqual(self.audio.default, "speaker")
        self.assertEqual(self.audio.streams[0]["sink"], 10)
        self.assertTrue(read_json(original.ROUTING_FILE)["restored"])
        self.assertFalse(any(s["index"] == 99 for s in self.audio.sinks))

    def test_verify_rejects_a_stale_ready_status_from_a_different_daemon(self):
        write_json(original.STATUS_FILE, {"ready": True, "state": "running", "daemon_pid": 111})
        with patch.object(original.converted_runtime, "is_active", return_value=True), \
             patch.object(original.converted_runtime, "systemctl", return_value=Result(0, "999\n", "")):
            self.assertFalse(original.verify(0))
        with patch.object(original.converted_runtime, "is_active", return_value=True), \
             patch.object(original.converted_runtime, "systemctl", return_value=Result(0, "111\n", "")):
            self.assertTrue(original.verify(0))

    def test_failed_stop_of_the_shared_converted_service_prevents_new_sink(self):
        with patch.object(original, "_configuration", return_value={"engine_profile": "speakers", "worker": {"command": ["worker"]}}), \
             patch.object(original, "ensure_unit"), \
             patch.object(original, "verify", return_value=False), \
             patch.object(original.converted_runtime, "is_active", return_value=True), \
             patch.object(original.converted_runtime, "stop", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "stop the previous"):
                original.start()
        self.assertEqual(self.audio.mutations(), [])
        self.assertFalse(Path(original.ROUTING_FILE).exists())

    def test_crash_before_module_id_commit_recovers_by_unique_sink_token(self):
        self.routed()
        journal = read_json(original.ROUTING_FILE)
        journal["module_id"] = None
        write_json(original.ROUTING_FILE, journal)
        original._restore_routing_locked()
        self.assertTrue(read_json(original.ROUTING_FILE)["restored"])
        self.assertEqual(self.audio.default, "usb")
        self.assertFalse(any(s["name"] == original.CAPTURE_SINK for s in self.audio.sinks))

    def test_deactivating_service_stop_failure_keeps_old_journal_and_cannot_prepare(self):
        self.routed()
        previous = Path(original.ROUTING_FILE).read_bytes()
        events = []
        with patch.object(original, "_configuration", return_value={"engine_profile": "speakers", "worker": {"command": ["worker"]}}), \
             patch.object(original, "ensure_unit", side_effect=lambda: events.append("unit")), \
             patch.object(original.converted_runtime, "is_active", return_value=False), \
             patch.object(original.converted_runtime, "stop", side_effect=lambda: events.append("stop") or False), \
             patch.object(original, "restore_routing") as restore, \
             patch.object(original, "_routing_action") as route, \
             patch.object(original.converted_runtime, "start") as start:
            with self.assertRaisesRegex(RuntimeError, "stop the previous audio service"):
                original.start()
        self.assertEqual(events, ["unit", "stop"])
        for command in (restore, route, start):
            command.assert_not_called()
        self.assertEqual(Path(original.ROUTING_FILE).read_bytes(), previous)
        self.assertEqual(self.audio.default, original.CAPTURE_SINK)
        self.assertEqual(self.audio.mutations(), [])

    def test_deactivating_service_finishes_old_cleanup_before_new_journal_is_created(self):
        self.routed()
        previous_token = read_json(original.ROUTING_FILE)["token"]
        events = []
        stopped = False
        def stop():
            nonlocal stopped
            events.append("stop-enter")
            self.assertEqual(read_json(original.ROUTING_FILE)["token"], previous_token)
            # Simulate the old unit's pending ExecStopPost, inside systemctl stop.
            original._restore_routing_locked()
            stopped = True
            events.append("old-cleanup-complete")
            return True
        def route(action):
            events.append(action)
            if action == "prepare":
                self.assertTrue(stopped, "A deactivating unit is not an inactive session")
                self.assertTrue(read_json(original.ROUTING_FILE)["restored"])
                self.assertFalse(any(s["name"] == original.CAPTURE_SINK for s in self.audio.sinks))
                original._prepare_route_locked()
            elif action == "route":
                original._route_after_ready_locked()
            else:
                self.fail("Old cleanup must complete before prepare, not run after the new journal")
        with patch.object(original, "_configuration", return_value={"engine_profile": "speakers", "worker": {"command": ["worker"]}}), \
             patch.object(original, "ensure_unit", side_effect=lambda: events.append("unit")), \
             patch.object(original.converted_runtime, "is_active", return_value=False), \
             patch.object(original.converted_runtime, "stop", side_effect=stop), \
             patch.object(original.converted_runtime, "start", side_effect=lambda: events.append("start") or True), \
             patch.object(original, "verify", return_value=True), \
             patch.object(original, "_routing_action", side_effect=route):
            self.assertTrue(original.start())
        self.assertEqual(events, ["unit", "stop-enter", "old-cleanup-complete", "prepare", "unit", "start", "route"])
        current = read_json(original.ROUTING_FILE)
        self.assertNotEqual(current["token"], previous_token)
        self.assertTrue(current["routed"])
        self.assertFalse(current["restored"])
        self.assertEqual(self.audio.default, original.CAPTURE_SINK)
        self.assertEqual(self.audio.streams[0]["sink"], 99)
        self.assertEqual([c[1] for c in self.audio.calls if c[1] in ("unload-module", "load-module")],
                         ["unload-module", "load-module"])


class OutputProfileTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="output-profile-test-", dir=ROOT / "tests")))
        self.engine_file = self.root / "engine.json"
        self.runtime_file = self.root / "runtime.json"
        self.routing_file = self.root / "routing.json"
        self.plugin = self.root / "plugin"
        bridge = self.plugin / "py_modules/allydsp/original/pipewire_bridge.py"
        bridge.parent.mkdir(parents=True)
        bridge.write_text("# public bridge fixture\n")
        asset = self.root / "private-profile-fixture.bin"
        asset.write_bytes(b"generated fixture; contains no vendor data")
        self.manifest = {
            "schema": "ally-dsp-original-engine-v1", "sample_rate": 48000, "input_channels": 6,
            "files": [{"path": str(asset), "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()}],
            "profiles": {
                "speakers": {"label": "Speaker Game", "worker": {"command": ["worker", "--speaker-game"]}},
                "headphones": {"label": "Headphone Game", "worker": {"command": ["worker", "--headphone-game"]}},
            },
        }
        self.route = {"name": "analog-output-speaker"}
        for target, key, value in [
            (original, "ENGINE_FILE", str(self.engine_file)),
            (original, "RUNTIME_FILE", str(self.runtime_file)),
            (original, "ROUTING_FILE", str(self.routing_file)),
            (original.paths, "PLUGIN_DIR", str(self.plugin)),
        ]:
            self.stack.enter_context(patch.object(target, key, value))
        self.stack.enter_context(patch.object(original.hardware, "pw_dump", return_value=[{}]))
        self.stack.enter_context(patch.object(original.hardware, "output_route", side_effect=lambda _: self.route))
        # Every process/service boundary is replaced, including on assertion failure.
        self.command = self.stack.enter_context(patch.object(original, "run", side_effect=AssertionError("Unexpected command")))
        write_json(self.engine_file, self.manifest)

    def test_analog_speaker_route_selects_official_speaker_worker(self):
        selected = original._configuration()
        self.assertEqual(selected["engine_profile"], "speakers")
        self.assertEqual(selected["worker"]["command"], ["worker", "--speaker-game"])
        self.assertEqual(selected["label"], "Speaker Game")
        self.assertEqual(read_json(self.engine_file), self.manifest, "Selection must not rewrite the imported pack")

    def test_analog_headphone_route_selects_separate_headphone_worker(self):
        self.route = {"name": "analog-output-headphones"}
        selected = original._configuration()
        self.assertEqual(selected["engine_profile"], "headphones")
        self.assertEqual(selected["worker"]["command"], ["worker", "--headphone-game"])
        self.assertEqual(selected["label"], "Headphone Game")

    def test_absent_headphone_route_selects_speakers(self):
        self.route = None
        self.assertEqual(original._configuration()["engine_profile"], "speakers")

    def test_missing_selected_profile_does_not_fall_back_to_headphone_worker(self):
        del self.manifest["profiles"]["speakers"]
        self.manifest["worker"] = {"command": ["legacy-headphone-worker"]}
        write_json(self.engine_file, self.manifest)
        with self.assertRaisesRegex(RuntimeError, "no configuration for this output"):
            original._configuration()
        self.command.assert_not_called()

    def test_legacy_single_profile_pack_keeps_its_explicit_headphone_identity(self):
        del self.manifest["profiles"]
        self.manifest["worker"] = {"command": ["legacy-headphone-worker"]}
        write_json(self.engine_file, self.manifest)
        selected = original._configuration()
        self.assertEqual(selected["engine_profile"], "headphones")
        self.assertEqual(selected["worker"]["command"], ["legacy-headphone-worker"])

    def test_ready_worker_with_same_profile_is_reused_without_route_mutation(self):
        write_json(self.runtime_file, {"engine_profile": "speakers"})
        with patch.object(original.converted_runtime, "is_active", return_value=True), \
             patch.object(original, "verify", return_value=True) as verify, \
             patch.object(original.converted_runtime, "stop") as stop, \
             patch.object(original.converted_runtime, "start") as start, \
             patch.object(original, "restore_routing") as restore, \
             patch.object(original, "_routing_action") as route, \
             patch.object(original, "ensure_unit") as unit:
            self.assertTrue(original.start())
        verify.assert_called_once_with(0)
        for command in (stop, start, restore, route, unit):
            command.assert_not_called()

    def test_ready_worker_restarts_when_output_profile_changes_in_either_direction(self):
        for previous, selected in [("headphones", "speakers"), ("speakers", "headphones"), (None, "speakers")]:
            with self.subTest(previous=previous, selected=selected):
                self.route = {"name": "analog-output-" + ("headphones" if selected == "headphones" else "speaker")}
                write_json(self.runtime_file, {"engine_profile": previous})
                events = []
                def route(action):
                    events.append(action)
                    if action == "prepare":
                        write_json(self.routing_file, {"physical_sink": "analog-sink"})
                with patch.object(original.converted_runtime, "is_active", return_value=True), \
                     patch.object(original, "verify", side_effect=[True, True]) as verify, \
                     patch.object(original.converted_runtime, "stop", side_effect=lambda: events.append("stop") or True), \
                     patch.object(original.converted_runtime, "start", side_effect=lambda: events.append("start") or True), \
                     patch.object(original, "restore_routing", side_effect=lambda: events.append("restore")), \
                     patch.object(original, "_routing_action", side_effect=route), \
                     patch.object(original, "ensure_unit", side_effect=lambda: events.append("unit")):
                    self.assertTrue(original.start())
                self.assertEqual(events, ["unit", "stop", "restore", "prepare", "unit", "start", "route"])
                self.assertEqual([call.args for call in verify.call_args_list], [(0,), (45,)])
                runtime = read_json(self.runtime_file)
                self.assertEqual(runtime["engine_profile"], selected)
                self.assertEqual(runtime["worker"], self.manifest["profiles"][selected]["worker"])
                self.assertEqual(runtime["playback_sink"], "analog-sink")

    def test_profile_change_cannot_replace_a_worker_that_failed_to_stop(self):
        write_json(self.runtime_file, {"engine_profile": "headphones"})
        with patch.object(original.converted_runtime, "is_active", return_value=True), \
             patch.object(original, "ensure_unit"), \
             patch.object(original, "verify", return_value=True), \
             patch.object(original.converted_runtime, "stop", return_value=False), \
             patch.object(original, "restore_routing") as restore, \
             patch.object(original, "_routing_action") as route:
            with self.assertRaisesRegex(RuntimeError, "stop the previous audio service"):
                original.start()
        restore.assert_not_called()
        route.assert_not_called()
        self.assertEqual(read_json(self.runtime_file)["engine_profile"], "headphones")


class SharedServiceTests(unittest.TestCase):
    def test_stop_restores_original_routing_for_either_backend(self):
        events = []
        with patch.object(dsp_runtime.converted_runtime, "stop", side_effect=lambda: events.append("stop") or True), \
             patch.object(dsp_runtime.original_runtime, "restore_routing", side_effect=lambda: events.append("restore")):
            self.assertTrue(dsp_runtime.stop())
        self.assertEqual(events, ["stop", "restore"])

    def test_restart_restores_before_installing_and_starting_selected_backend(self):
        events = []
        with patch.object(dsp_runtime, "engine_kind", return_value="original"), \
             patch.object(dsp_runtime.converted_runtime, "stop", side_effect=lambda: events.append("stop") or True), \
             patch.object(dsp_runtime.original_runtime, "restore_routing", side_effect=lambda: events.append("restore")), \
             patch.object(dsp_runtime.original_runtime, "ensure_unit", side_effect=lambda: events.append("original-unit")), \
             patch.object(dsp_runtime.original_runtime, "start", side_effect=lambda: events.append("original-start") or True):
            self.assertTrue(dsp_runtime.restart())
        self.assertEqual(events, ["stop", "restore", "original-unit", "original-start"])

    def test_headphone_pause_policy_is_specific_to_converted_engine(self):
        with patch.object(dsp_runtime, "engine_kind", return_value="converted"):
            self.assertTrue(dsp_runtime.pause_for_headphones())
        with patch.object(dsp_runtime, "engine_kind", return_value="original"):
            self.assertFalse(dsp_runtime.pause_for_headphones())


class RecoveryHelperTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="recovery-helper-test-", dir=ROOT / "tests")))
        self.plugin = self.root / "plugin"
        self.source = self.plugin / "py_modules/allydsp"
        self.source.mkdir(parents=True)
        self.source_files = {"__init__.py": b"", "original_recover.py": b"# public recovery fixture\n",
                             "original_runtime.py": b"# public runtime fixture\n", "paths.py": b"VALUE = 1\n"}
        for name, content in self.source_files.items():
            (self.source / name).write_bytes(content)
        (self.source / "original").mkdir()
        (self.source / "original" / "worker.py").write_text("# nested worker must not be copied")
        (self.source / "private.dll").write_bytes(b"generated dummy fixture, no vendor data")
        (self.source / "private.bin").write_bytes(b"generated dummy fixture")
        patches = [(original, "ORIGINAL_DIR", str(self.root / "runtime/original")),
                   (original.paths, "PLUGIN_DIR", str(self.plugin)),
                   (original.paths, "RUNTIME_DIR", str(self.root / "runtime")),
                   (original.paths, "UNIT_PATH", str(self.root / "unit/ally-dsp.service")),
                   (original.paths, "HOME", str(self.root / "home")),
                   (original.paths, "USER", "fixture"),
                   (original.paths, "SETTINGS_DIR", str(self.root / "settings")),
                   (original.paths, "LOG_DIR", str(self.root / "logs")),
                   (original.paths, "SYSTEM_PYTHON", "/usr/bin/python3")]
        for target, name, value in patches:
            self.stack.enter_context(patch.object(target, name, value))

    def remove_fixture_plugin(self):
        target = self.plugin.resolve()
        self.assertTrue(target.is_relative_to(self.root.resolve()))
        self.assertNotEqual(target, self.root.resolve())
        shutil.rmtree(target)

    def test_only_public_top_level_python_modules_are_retained(self):
        original._ensure_recovery()
        destination = Path(original._recovery_script()).parent
        self.assertEqual({p.name for p in destination.iterdir()}, set(self.source_files))
        for name, content in self.source_files.items():
            self.assertEqual((destination / name).read_bytes(), content)
        if os.name == "posix":
            self.assertEqual((destination / "original_recover.py").stat().st_mode & 0o777, 0o600)

    def test_existing_recovery_survives_plugin_code_removal(self):
        original._ensure_recovery()
        helper = Path(original._recovery_script())
        previous = helper.read_bytes()
        self.remove_fixture_plugin()
        original._ensure_recovery()
        self.assertEqual(helper.read_bytes(), previous)

    def test_missing_plugin_and_missing_recovery_fail_without_running_a_command(self):
        self.remove_fixture_plugin()
        with patch.object(original, "run") as command:
            with self.assertRaisesRegex(RuntimeError, "recovery helper is unavailable"):
                original._routing_action("restore")
            command.assert_not_called()

    def test_routing_uses_retained_helper_and_explicit_runtime_environment(self):
        original._ensure_recovery()
        self.remove_fixture_plugin()
        with patch.object(original, "run", return_value=Result(0, "", "")) as command, \
             patch.object(original, "user_env", side_effect=lambda value: value):
            original._routing_action("restore")
        args, kwargs = command.call_args
        self.assertEqual(args[0], ["/usr/bin/python3", original._recovery_script(), "--action", "restore"])
        self.assertEqual(kwargs["env"]["DECKY_PLUGIN_RUNTIME_DIR"], original.paths.RUNTIME_DIR)
        self.assertEqual(kwargs["env"]["ALLYDSP_PLUGIN_DIR"], str(self.plugin))

    def test_installed_unit_can_recover_after_the_plugin_path_is_deleted(self):
        with patch.object(original.converted_runtime, "systemctl", return_value=Result(0, "", "")):
            original.install_unit()
        unit = Path(original.paths.UNIT_PATH).read_text()
        private_helper = original._unit_arg(original._recovery_script())
        self.assertIn('ExecStopPost="/usr/bin/python3" ' + private_helper, unit)
        self.assertIn('Environment=' + original._unit_arg('DECKY_PLUGIN_RUNTIME_DIR=' + original.paths.RUNTIME_DIR), unit)
        self.remove_fixture_plugin()
        self.assertTrue(Path(original._recovery_script()).is_file())


class JackLifecycleLockTests(unittest.TestCase):
    def test_jack_transition_waits_for_backend_switch_to_release_shared_lock(self):
        from allydsp import jackwatch
        calls = []
        async def scenario():
            lock = asyncio.Lock()
            watcher = jackwatch.JackWatcher(lifecycle_lock=lock)
            watcher.headphones = False
            await lock.acquire()
            task = asyncio.create_task(watcher.poll(lambda: True))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(calls, [], "No device query or service change is allowed during a switch")
            lock.release()
            await asyncio.wait_for(task, 2)
            self.assertEqual(calls, ["query", "stop"])
        with patch.object(jackwatch.hardware, "pw_dump", side_effect=lambda: calls.append("query") or [{}]), \
             patch.object(jackwatch.hardware, "output_route", return_value=None), \
             patch.object(jackwatch.hardware, "headphones_active", return_value=True), \
             patch.object(jackwatch.dsp_runtime, "pause_for_headphones", return_value=True), \
             patch.object(jackwatch.dsp_runtime, "is_active", return_value=True), \
             patch.object(jackwatch.dsp_runtime, "stop", side_effect=lambda: calls.append("stop")):
            asyncio.run(scenario())

    def test_cancelled_waiting_jack_poll_cannot_mutate_the_service(self):
        from allydsp import jackwatch
        calls = []
        async def scenario():
            lock = asyncio.Lock()
            watcher = jackwatch.JackWatcher(lifecycle_lock=lock)
            await lock.acquire()
            task = asyncio.create_task(watcher.poll(lambda: True))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            lock.release()
            self.assertIsNone(watcher.headphones)
            self.assertEqual(calls, [])
        with patch.object(jackwatch.hardware, "pw_dump", side_effect=lambda: calls.append("query")):
            asyncio.run(scenario())


class BackendSwitchTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        decky = types.ModuleType("decky")
        decky.logger = logging.getLogger("original-runtime-switch-test")
        decky.DECKY_PLUGIN_VERSION = "test"
        async def emit(*args):
            pass
        decky.emit = emit
        self.stack.enter_context(patch.dict(sys.modules, {"decky": decky}))
        spec = importlib.util.spec_from_file_location("ally_original_switch_test", ROOT / "main.py")
        self.main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.main)
        self.plugin = self.main.Plugin()
        self.plugin.settings = {"engine": {"kind": "converted"}, "enabled": True,
                                "setup": {"done": True}}
        self.plugin.setup_task = self.plugin.convert_task = None
        self.events = []
        self.stack.enter_context(patch.object(self.main.dsp_runtime, "engine_kind",
                                              lambda: self.plugin.settings["engine"]["kind"]))
        self.stack.enter_context(patch.object(self.main.dsp_runtime, "is_active", return_value=True))
        self.stack.enter_context(patch.object(self.main.original_runtime, "availability",
                                              return_value={"available": True}))
        self.stack.enter_context(patch.object(self.main.dsp_runtime, "stop",
            side_effect=lambda: self.events.append("stop:" + self.plugin.settings["engine"]["kind"]) or True))
        self.stack.enter_context(patch.object(self.main.dsp_runtime, "ensure_unit",
            side_effect=lambda: self.events.append("unit:" + self.plugin.settings["engine"]["kind"])))
        self.stack.enter_context(patch.object(self.main.dsp_runtime, "enable",
            side_effect=lambda value: self.events.append("enable:" + self.plugin.settings["engine"]["kind"] + ":" + str(value))))
        self.plugin._save = lambda: self.events.append("save:" + self.plugin.settings["engine"]["kind"])

    def switch(self):
        async def scenario():
            self.plugin.engine_lock = asyncio.Lock()
            return await self.plugin.set_engine("original")
        return asyncio.run(scenario())

    def test_missing_original_assets_do_not_stop_the_working_engine(self):
        with patch.object(self.main.original_runtime, "availability",
                          return_value={"available": False, "error": "missing local assets"}):
            with self.assertRaisesRegex(RuntimeError, "missing local assets"):
                self.switch()
        self.assertEqual(self.events, [])
        self.assertEqual(self.plugin.settings["engine"]["kind"], "converted")

    def test_failed_switch_restores_the_saved_engine_and_previous_active_service(self):
        async def apply(**kwargs):
            kind = self.plugin.settings["engine"]["kind"]
            self.events.append("apply:" + kind)
            if kind == "original":
                raise RuntimeError("worker startup failed")
            return {"verified": True}
        self.plugin._apply_current_locked = apply
        with self.assertRaisesRegex(RuntimeError, "worker startup failed; previous engine restored"):
            self.switch()
        self.assertEqual(self.plugin.settings["engine"]["kind"], "converted")
        self.assertEqual(self.events, ["stop:converted", "save:original", "unit:original", "enable:original:True", "apply:original",
                                      "stop:original", "save:converted", "unit:converted", "enable:converted:True", "apply:converted"])

    def test_failed_recovery_is_reported_instead_of_claiming_success(self):
        async def apply(**kwargs):
            kind = self.plugin.settings["engine"]["kind"]
            self.events.append("apply:" + kind)
            raise RuntimeError("original failed" if kind == "original" else "converted failed")
        self.plugin._apply_current_locked = apply
        with self.assertRaisesRegex(RuntimeError, "original failed; recovery failed: converted failed"):
            self.switch()
        self.assertEqual(self.plugin.settings["engine"]["kind"], "converted")

    def test_original_jack_change_reapplies_profile_while_shared_lock_is_held(self):
        self.plugin.settings["engine"]["kind"] = "original"
        self.plugin.settings["setup"]["done"] = False
        async def apply(**kwargs):
            self.assertTrue(self.plugin.engine_lock.locked())
            self.events.append("apply:original")
        async def state():
            return {"verified": True}
        self.plugin._apply_current_locked = apply
        self.plugin._dsp_state = state
        async def scenario():
            self.plugin.engine_lock = asyncio.Lock()
            async with self.plugin.engine_lock:
                await asyncio.wait_for(self.plugin._on_jack_change({"headphones": True}), 2)
        asyncio.run(scenario())
        self.assertEqual(self.events, ["apply:original"])

    def test_disabled_original_and_converted_jack_callbacks_do_not_apply_original_profile(self):
        async def apply(**kwargs):
            self.fail("An inactive original engine must not be started by a jack callback")
        async def state():
            return {}
        self.plugin._apply_current_locked = apply
        self.plugin._dsp_state = state
        for kind, enabled in [("original", False), ("converted", True)]:
            with self.subTest(kind=kind, enabled=enabled):
                self.plugin.settings.update(engine={"kind": kind}, enabled=enabled)
                asyncio.run(self.plugin._on_jack_change({"headphones": False}))
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()

