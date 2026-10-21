"""Failure/recovery semantics with an in-memory Pulse session and shared service.

These tests never invoke pactl/systemd or change the host audio session. They
also run with unittest so the offline Windows research runtime needs no pytest.
"""
import asyncio
import copy
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
    @staticmethod
    def volume(values, *, surround=False):
        channels = ["front-left", "front-right", "front-center", "lfe", "rear-left", "rear-right"] if surround else ["front-left", "front-right"]
        if not isinstance(values, list):
            values = [values] * len(channels)
        assert len(values) == len(channels)
        return {channel: {"value": value} for channel, value in zip(channels, values)}

    def __init__(self):
        self.sinks = [
            {"name": "speaker", "index": 10, "owner_module": 1, "properties": {"object.serial": "110"}, "volume": self.volume(65536), "mute": False},
            {"name": "usb", "index": 20, "owner_module": 2, "properties": {"object.serial": "120"}, "volume": self.volume(65536), "mute": False},
        ]
        self.streams = [{"index": 100, "sink": 10, "properties": {
            "application.process.id": "123", "media.name": "Game", "object.serial": "1000"}}]
        self.default = "speaker"
        self.calls = []
        self.fail_move = set()
        self.disappear_on_move = set()

    def owned(self, token="session", module=42):
        self.sinks.append({"name": original.CAPTURE_SINK, "index": 99,
                           "owner_module": module, "properties": {"allydsp.session": token},
                           "volume": self.volume(65536, surround=True), "mute": False})

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
        elif op == "set-sink-input-volume":
            next(s for s in self.streams if s["index"] == int(args[2]))["volume_raw"] = int(args[3])
        elif op == "set-sink-volume":
            sink = next(s for s in self.sinks if s["name"] == args[2])
            values = [int(value) for value in args[3:]]
            if len(values) == 1:
                values *= len(sink["volume"])
            assert len(values) == len(sink["volume"])
            for channel, value in zip(sink["volume"], values):
                sink["volume"][channel]["value"] = value
        elif op == "set-sink-mute":
            sink = next(s for s in self.sinks if s["name"] == args[2])
            assert args[3] in ("0", "1", "false", "true")
            sink["mute"] = args[3] in ("1", "true")
        else:
            raise AssertionError("Unexpected pactl command: " + repr(args))
        return Result(0, "", "")

    def mutations(self):
        return [x for x in self.calls if x[1] in
                ("set-default-sink", "move-sink-input", "load-module", "unload-module", "set-sink-input-volume", "set-sink-volume", "set-sink-mute")]


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
        self.stack.enter_context(patch.object(original, "apply_output_gain", return_value={"applied": False}))
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

    def sink(self, name="speaker"):
        return next(sink for sink in self.audio.sinks if sink["name"] == name)

    def levels(self, name="speaker"):
        return [channel["value"] for channel in self.sink(name)["volume"].values()]

    def activate_handoff(self, *, volume=34734, mute=False):
        self.sink()["volume"] = self.audio.volume(volume)
        self.sink()["mute"] = mute
        self.prepare_v1()
        with patch.object(original, "verify", return_value=True):
            original._route_after_ready_locked()

    def prepare_v1(self):
        """Prepare a real owned sink, then load an explicit historical v1 journal.

        New sessions use v2. These fixtures preserve the old unity handoff and
        its crash recovery coverage without asking new production code to
        generate obsolete v1 sessions.
        """
        original._prepare_route_locked()
        journal = read_json(original.ROUTING_FILE)
        if journal["schema"] == "ally-dsp-original-routing-v1":
            return
        self.assertEqual(journal["schema"], "ally-dsp-original-routing-v2")
        physical = self.sink()
        journal["schema"] = "ally-dsp-original-routing-v1"
        journal.pop("master_mirror")
        journal["volume_handoff"] = {
            "schema": "ally-dsp-original-volume-handoff-v1",
            "physical_identity": {"name": physical["name"], "index": physical["index"],
                                  "serial": physical["properties"]["object.serial"]},
            "original": {"volume": self.levels(), "mute": physical["mute"]},
            "virtual_initial": {"volume": self.levels(original.CAPTURE_SINK),
                                "mute": self.sink(original.CAPTURE_SINK)["mute"]},
            "volume": {"intent": False, "applied": False},
            "mute": {"intent": False, "applied": False},
        }
        write_json(original.ROUTING_FILE, journal)

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
        self.assertEqual(self.audio.mutations()[0][1], "load-module")
        self.assertTrue(all(c[1] in ("set-sink-volume", "set-sink-mute") and c[2] == original.CAPTURE_SINK
                            for c in self.audio.mutations()[1:]), "Preparation must leave games, default and physical master alone")

    def test_virtual_master_inherits_physical_level_mute_and_stereo_balance_before_ready(self):
        self.sink()["volume"] = self.audio.volume([32768, 49152])
        self.sink()["mute"] = True
        previous = copy.deepcopy(self.sink())
        self.prepare_v1()
        center = round(((32768 ** 3 + 49152 ** 3) / 2) ** (1 / 3))
        self.assertEqual(self.levels(original.CAPTURE_SINK), [32768, 49152, center, center, 32768, 49152])
        self.assertTrue(self.sink(original.CAPTURE_SINK)["mute"])
        self.assertEqual(self.sink(), previous)
        self.assertEqual(self.audio.default, "speaker")
        self.assertEqual(self.audio.streams[0]["sink"], 10)
        handoff = read_json(original.ROUTING_FILE)["volume_handoff"]
        self.assertEqual(handoff["original"], {"volume": [32768, 49152], "mute": True})
        self.assertFalse(handoff["volume"]["intent"])
        self.assertFalse(handoff["mute"]["intent"])

    def test_volume_change_during_worker_startup_is_resynced_before_route_and_restored(self):
        self.sink()["volume"] = self.audio.volume(34734)
        self.prepare_v1()
        self.assertEqual(self.levels(original.CAPTURE_SINK), [34734] * 6)
        # The user changes the still-default physical output while the worker initializes.
        self.sink()["volume"] = self.audio.volume(39322)
        self.sink()["mute"] = True
        with patch.object(original, "verify", return_value=True):
            original._route_after_ready_locked()
        self.assertEqual(self.levels(original.CAPTURE_SINK), [39322] * 6)
        self.assertTrue(self.sink(original.CAPTURE_SINK)["mute"])
        self.assertEqual(self.levels(), [65536, 65536])
        self.assertFalse(self.sink()["mute"])
        self.assertEqual(read_json(original.ROUTING_FILE)["volume_handoff"]["original"],
                         {"volume": [39322, 39322], "mute": True})
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [39322, 39322])
        self.assertTrue(self.sink()["mute"])

    def test_repeating_active_route_does_not_replace_virtual_master_with_physical_unity(self):
        self.activate_handoff(volume=34734)
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(40000, surround=True)
        self.sink(original.CAPTURE_SINK)["mute"] = True
        with patch.object(original, "verify", return_value=True):
            original._route_after_ready_locked()
        self.assertEqual(self.levels(original.CAPTURE_SINK), [40000] * 6)
        self.assertTrue(self.sink(original.CAPTURE_SINK)["mute"])
        self.assertEqual(self.levels(), [65536, 65536])
        self.assertFalse(self.sink()["mute"])

    def test_virtual_only_startup_volume_and_mute_changes_are_preserved(self):
        self.sink()["volume"] = self.audio.volume(34734)
        self.prepare_v1()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        self.sink(original.CAPTURE_SINK)["mute"] = True
        with patch.object(original, "verify", return_value=True):
            original._route_after_ready_locked()
        self.assertEqual(self.levels(original.CAPTURE_SINK), [45000] * 6)
        self.assertTrue(self.sink(original.CAPTURE_SINK)["mute"])
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [45000, 45000])
        self.assertTrue(self.sink()["mute"])

    def test_conflicting_startup_volume_changes_fail_before_games_or_default_move(self):
        self.sink()["volume"] = self.audio.volume(34734)
        self.prepare_v1()
        self.sink()["volume"] = self.audio.volume(39322)
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        before = len(self.audio.calls)
        with patch.object(original, "verify", return_value=True):
            with self.assertRaises(RuntimeError):
                original._route_after_ready_locked()
        self.assertEqual(self.audio.default, "speaker")
        self.assertEqual(self.audio.streams[0]["sink"], 10)
        self.assertEqual(self.levels(), [39322, 39322])
        self.assertFalse(any(c[1] in ("set-default-sink", "move-sink-input") for c in self.audio.calls[before:]))
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [39322, 39322])

    def test_independent_startup_physical_volume_and_virtual_mute_changes_are_merged(self):
        self.sink()["volume"] = self.audio.volume(34734)
        self.prepare_v1()
        self.sink()["volume"] = self.audio.volume(39322)
        self.sink(original.CAPTURE_SINK)["mute"] = True
        with patch.object(original, "verify", return_value=True):
            original._route_after_ready_locked()
        self.assertEqual(self.levels(original.CAPTURE_SINK), [39322] * 6)
        self.assertTrue(self.sink(original.CAPTURE_SINK)["mute"])
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [39322, 39322])
        self.assertTrue(self.sink()["mute"])

    def test_equal_startup_volume_changes_on_both_masters_are_not_a_conflict(self):
        self.sink()["volume"] = self.audio.volume(34734)
        self.prepare_v1()
        self.sink()["volume"] = self.audio.volume(39322)
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(39322, surround=True)
        with patch.object(original, "verify", return_value=True):
            original._route_after_ready_locked()
        self.assertEqual(self.levels(original.CAPTURE_SINK), [39322] * 6)
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [39322, 39322])

    def test_legacy_routing_journal_restores_routes_without_owning_any_master_volume(self):
        self.routed()  # A pre-handoff journal has no volume_handoff section.
        self.sink()["volume"] = self.audio.volume([30000, 35000])
        self.sink()["mute"] = True
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [30000, 35000])
        self.assertTrue(self.sink()["mute"])
        self.assertFalse(any(c[1] in ("set-sink-volume", "set-sink-mute") for c in self.audio.calls))
        self.assertTrue(read_json(original.ROUTING_FILE)["restored"])

    def test_physical_unity_and_unmute_happen_only_after_games_and_default_move(self):
        self.sink()["volume"] = self.audio.volume(34734)
        self.sink()["mute"] = True
        self.prepare_v1()
        events = []
        def run(args, **kwargs):
            if args[1] in ("set-sink-volume", "set-sink-mute") and args[2] == "speaker":
                self.assertEqual(self.audio.default, original.CAPTURE_SINK)
                self.assertEqual(self.audio.streams[0]["sink"], 99)
                key = "volume" if args[1] == "set-sink-volume" else "mute"
                stage = read_json(original.ROUTING_FILE)["volume_handoff"][key]
                self.assertTrue(stage["intent"], "A completed command must remain recoverable after a crash")
                self.assertFalse(stage["applied"])
                events.append(key)
            return self.audio.run(args, **kwargs)
        with patch.object(original, "run", side_effect=run), patch.object(original, "verify", return_value=True):
            original._route_after_ready_locked()
        self.assertEqual(events, ["volume", "mute"])
        self.assertEqual(self.levels(), [65536, 65536])
        self.assertFalse(self.sink()["mute"])
        self.assertEqual(self.levels(original.CAPTURE_SINK), [34734] * 6)
        self.assertTrue(self.sink(original.CAPTURE_SINK)["mute"], "An initially muted output must stay muted to the listener")

    def test_restore_transfers_current_virtual_volume_and_mute_back_before_unload(self):
        self.activate_handoff()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        self.sink(original.CAPTURE_SINK)["mute"] = True
        def run(args, **kwargs):
            if args[1] == "unload-module":
                self.assertEqual(self.levels(), [45000, 45000])
                self.assertTrue(self.sink()["mute"])
            return self.audio.run(args, **kwargs)
        with patch.object(original, "run", side_effect=run):
            original._restore_routing_locked()
        self.assertEqual(self.audio.default, "speaker")
        self.assertEqual(self.audio.streams[0]["sink"], 10)
        self.assertTrue(read_json(original.ROUTING_FILE)["restored"])

    def test_restore_respects_external_physical_volume_change_but_still_restores_mute(self):
        self.activate_handoff()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        self.sink(original.CAPTURE_SINK)["mute"] = True
        self.sink()["volume"] = self.audio.volume([30000, 31000])
        before = len(self.audio.calls)
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [30000, 31000])
        self.assertTrue(self.sink()["mute"])
        self.assertFalse(any(c[1] == "set-sink-volume" and c[2] == "speaker" for c in self.audio.calls[before:]))
        self.assertTrue(read_json(original.ROUTING_FILE)["volume_handoff"]["restore"]["volume"]["preserved_external"])

    def test_restore_respects_external_physical_mute_change_but_still_restores_volume(self):
        self.activate_handoff()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        self.sink(original.CAPTURE_SINK)["mute"] = False
        self.sink()["mute"] = True
        before = len(self.audio.calls)
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [45000, 45000])
        self.assertTrue(self.sink()["mute"])
        self.assertFalse(any(c[1] == "set-sink-mute" and c[2] == "speaker" for c in self.audio.calls[before:]))
        self.assertTrue(read_json(original.ROUTING_FILE)["volume_handoff"]["restore"]["mute"]["preserved_external"])

    def test_virtual_sink_disappearance_restores_original_physical_level_instead_of_unity(self):
        self.activate_handoff(volume=34734, mute=True)
        self.audio.sinks = [sink for sink in self.audio.sinks if sink["name"] != original.CAPTURE_SINK]
        self.audio.streams = []
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [34734, 34734])
        self.assertTrue(self.sink()["mute"])
        self.assertEqual(self.audio.default, "speaker")

    def test_crash_after_physical_command_before_applied_commit_is_recoverable(self):
        class Crash(BaseException):
            pass
        for operation in ("set-sink-volume", "set-sink-mute"):
            with self.subTest(operation=operation):
                # Start each subcase with a distinct clean session and journal.
                self.audio = AudioSession()
                original.run = self.audio.run
                Path(original.ROUTING_FILE).unlink(missing_ok=True)
                self.sink()["volume"] = self.audio.volume(34734)
                self.sink()["mute"] = True
                self.prepare_v1()
                def crash(args, **kwargs):
                    result = self.audio.run(args, **kwargs)
                    if args[1] == operation and args[2] == "speaker":
                        raise Crash()
                    return result
                with patch.object(original, "run", side_effect=crash), patch.object(original, "verify", return_value=True):
                    with self.assertRaises(Crash):
                        original._route_after_ready_locked()
                key = "volume" if operation == "set-sink-volume" else "mute"
                pending = read_json(original.ROUTING_FILE)["volume_handoff"][key]
                self.assertTrue(pending["intent"])
                self.assertFalse(pending["applied"])
                original._restore_routing_locked()
                self.assertEqual(self.levels(), [34734, 34734])
                self.assertTrue(self.sink()["mute"])
                self.assertTrue(read_json(original.ROUTING_FILE)["restored"])

    def test_restore_target_is_journaled_before_write_and_does_not_follow_later_virtual_changes(self):
        class Crash(BaseException):
            pass
        self.activate_handoff()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(43000, surround=True)
        def crash(args, **kwargs):
            if args[1] == "set-sink-volume" and args[2] == "speaker":
                restore = read_json(original.ROUTING_FILE)["volume_handoff"]["restore"]["volume"]
                self.assertTrue(restore["intent"])
                self.assertEqual(restore["target"], [43000, 43000])
                result = self.audio.run(args, **kwargs)
                raise Crash()
            return self.audio.run(args, **kwargs)
        with patch.object(original, "run", side_effect=crash):
            with self.assertRaises(Crash):
                original._restore_routing_locked()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(55000, surround=True)
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [43000, 43000])
        self.assertTrue(read_json(original.ROUTING_FILE)["restored"])

    def test_missing_physical_serial_fails_before_creating_or_changing_audio_nodes(self):
        del self.sink()["properties"]["object.serial"]
        with self.assertRaises(RuntimeError):
            self.prepare_v1()
        self.assertEqual(self.audio.mutations(), [])

    def test_replacement_physical_identity_does_not_receive_saved_master_settings(self):
        self.activate_handoff()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        self.sink(original.CAPTURE_SINK)["mute"] = True
        self.sink()["index"] = 30
        self.sink()["properties"]["object.serial"] = "replacement"
        before = len(self.audio.calls)
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [65536, 65536])
        self.assertFalse(self.sink()["mute"])
        self.assertFalse(any(c[1] in ("set-sink-volume", "set-sink-mute") and c[2] == "speaker"
                             for c in self.audio.calls[before:]))

    def test_failed_intent_journal_write_cannot_change_physical_master(self):
        self.sink()["volume"] = self.audio.volume(34734)
        self.sink()["mute"] = True
        self.prepare_v1()
        real_write = original.write_json
        def fail(path, value):
            stage = value.get("volume_handoff", {}).get("volume", {})
            if path == original.ROUTING_FILE and stage.get("intent") and not stage.get("applied"):
                raise OSError("journal unavailable")
            return real_write(path, value)
        before = len(self.audio.calls)
        with patch.object(original, "write_json", side_effect=fail), patch.object(original, "verify", return_value=True):
            with self.assertRaisesRegex(OSError, "journal unavailable"):
                original._route_after_ready_locked()
        self.assertEqual(self.levels(), [34734, 34734])
        self.assertTrue(self.sink()["mute"])
        self.assertFalse(any(c[1] in ("set-sink-volume", "set-sink-mute") and c[2] == "speaker"
                             for c in self.audio.calls[before:]))
        original._restore_routing_locked()
        self.assertTrue(read_json(original.ROUTING_FILE)["restored"])

    def test_failed_physical_handoff_command_can_restore_without_leaving_unity(self):
        for operation in ("set-sink-volume", "set-sink-mute"):
            with self.subTest(operation=operation):
                self.audio = AudioSession()
                original.run = self.audio.run
                Path(original.ROUTING_FILE).unlink(missing_ok=True)
                self.sink()["volume"] = self.audio.volume(34734)
                self.sink()["mute"] = True
                self.prepare_v1()
                def fail(args, **kwargs):
                    if args[1] == operation and args[2] == "speaker":
                        return Result(1, "", "physical volume command rejected")
                    return self.audio.run(args, **kwargs)
                with patch.object(original, "run", side_effect=fail), patch.object(original, "verify", return_value=True):
                    with self.assertRaisesRegex(RuntimeError, "physical volume command rejected"):
                        original._route_after_ready_locked()
                original._restore_routing_locked()
                self.assertEqual(self.levels(), [34734, 34734])
                self.assertTrue(self.sink()["mute"])
                self.assertEqual(self.audio.default, "speaker")
                self.assertTrue(read_json(original.ROUTING_FILE)["restored"])

    def test_failed_master_restore_keeps_virtual_sink_and_journal_for_retry(self):
        self.activate_handoff()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        def fail(args, **kwargs):
            if args[1] == "set-sink-volume" and args[2] == "speaker":
                return Result(1, "", "restore volume rejected")
            return self.audio.run(args, **kwargs)
        before = len(self.audio.calls)
        with patch.object(original, "run", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "restore volume rejected"):
                original._restore_routing_locked()
        self.assertFalse(read_json(original.ROUTING_FILE)["restored"])
        self.assertEqual(self.audio.default, original.CAPTURE_SINK)
        self.assertTrue(any(s["name"] == original.CAPTURE_SINK for s in self.audio.sinks))
        self.assertFalse(any(c[1] == "unload-module" for c in self.audio.calls[before:]))
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [45000, 45000])
        self.assertTrue(read_json(original.ROUTING_FILE)["restored"])

    def test_repeated_handoff_cycles_transfer_master_values_without_accumulation(self):
        self.activate_handoff(volume=34734)
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(40000, surround=True)
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [40000, 40000])
        self.prepare_v1()
        self.assertEqual(self.levels(original.CAPTURE_SINK), [40000] * 6)
        with patch.object(original, "verify", return_value=True):
            original._route_after_ready_locked()
        self.assertEqual(self.levels(), [65536, 65536])
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [40000, 40000])

    def test_uncommitted_unity_rounding_after_crash_still_restores_original_master(self):
        class Crash(BaseException):
            pass
        self.sink()["volume"] = self.audio.volume(34734)
        self.prepare_v1()
        def crash(args, **kwargs):
            result = self.audio.run(args, **kwargs)
            if args[1] == "set-sink-volume" and args[2] == "speaker":
                self.sink()["volume"] = self.audio.volume(65533)
                raise Crash()
            return result
        with patch.object(original, "run", side_effect=crash), patch.object(original, "verify", return_value=True):
            with self.assertRaises(Crash):
                original._route_after_ready_locked()
        pending = read_json(original.ROUTING_FILE)["volume_handoff"]["volume"]
        self.assertFalse(pending["applied"])
        self.assertEqual(pending["expected"], [65536, 65536])
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [34734, 34734])
        self.assertTrue(read_json(original.ROUTING_FILE)["restored"])

    def test_committed_unity_uses_actual_readback_and_preserves_even_small_external_changes(self):
        self.sink()["volume"] = self.audio.volume(34734)
        self.prepare_v1()
        def rounded(args, **kwargs):
            result = self.audio.run(args, **kwargs)
            if args[1] == "set-sink-volume" and args[2] == "speaker":
                self.sink()["volume"] = self.audio.volume(65533)
            return result
        with patch.object(original, "run", side_effect=rounded), patch.object(original, "verify", return_value=True):
            original._route_after_ready_locked()
        committed = read_json(original.ROUTING_FILE)["volume_handoff"]["volume"]
        self.assertTrue(committed["applied"])
        self.assertEqual(committed["expected"], [65533, 65533])
        self.sink()["volume"] = self.audio.volume(65536)
        before = len(self.audio.calls)
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [65536, 65536])
        self.assertFalse(any(c[1] == "set-sink-volume" and c[2] == "speaker" for c in self.audio.calls[before:]))
        self.assertTrue(read_json(original.ROUTING_FILE)["volume_handoff"]["restore"]["volume"]["preserved_external"])

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

    def test_output_gain_is_applied_before_any_game_stream_moves_into_capture(self):
        original._prepare_route_locked()
        def gain():
            self.assertEqual(self.audio.default, "speaker")
            self.assertEqual(self.audio.streams[0]["sink"], 10)
            self.assertEqual(read_json(original.ROUTING_FILE)["moved"], [])
            return {"applied": True}
        with patch.object(original, "verify", return_value=True), \
             patch.object(original, "apply_output_gain", side_effect=gain) as apply:
            original._route_after_ready_locked()
        apply.assert_called_once_with()
        self.assertEqual(self.audio.streams[0]["sink"], 99)

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


class PhysicalMasterMirrorTests(unittest.TestCase):
    """v2 keeps hardware master authoritative without a unity handoff."""

    setUp = OriginalRoutingTests.setUp
    sink = OriginalRoutingTests.sink
    levels = OriginalRoutingTests.levels

    @staticmethod
    def surround(left, right):
        center = round(((left ** 3 + right ** 3) / 2) ** (1 / 3))
        return [left, right, center, center, left, right]

    def activate(self, *, volume=34734, mute=False):
        self.sink()["volume"] = self.audio.volume(volume)
        self.sink()["mute"] = mute
        original._prepare_route_locked()
        with patch.object(original, "verify", return_value=True):
            original._route_after_ready_locked()
        self.assertEqual(read_json(original.ROUTING_FILE)["schema"], "ally-dsp-original-routing-v2")
        self.audio.calls.clear()

    def masters(self):
        return [call for call in self.audio.mutations() if call[1] in ("set-sink-volume", "set-sink-mute")]

    def test_prepare_mirrors_stereo_balance_and_mute_without_attenuating_monitor_pcm(self):
        self.sink()["volume"] = self.audio.volume([30000, 40000])
        self.sink()["mute"] = True
        original._prepare_route_locked()
        journal = read_json(original.ROUTING_FILE)
        self.assertEqual(journal["schema"], "ally-dsp-original-routing-v2")
        self.assertNotIn("volume_handoff", journal)
        self.assertEqual(self.levels(), [30000, 40000])
        self.assertTrue(self.sink()["mute"])
        self.assertEqual(self.levels(original.CAPTURE_SINK), self.surround(30000, 40000))
        self.assertTrue(self.sink(original.CAPTURE_SINK)["mute"])
        load = next(call for call in self.audio.calls if call[1] == "load-module")
        self.assertIn("monitor.channel-volumes=false", load[-1])
        self.assertFalse(any(call[2] == "speaker" for call in self.masters()))
        self.assertEqual(journal["master_mirror"]["physical_identity"],
                         {"name": "speaker", "index": 10, "serial": "110"})

    def test_ready_resyncs_latest_physical_without_unity_or_unmute(self):
        self.sink()["volume"] = self.audio.volume(34734)
        original._prepare_route_locked()
        self.sink()["volume"] = self.audio.volume(40000)
        self.sink()["mute"] = True
        self.audio.calls.clear()
        with patch.object(original, "verify", return_value=True):
            original._route_after_ready_locked()
        self.assertEqual(self.levels(), [40000, 40000])
        self.assertTrue(self.sink()["mute"])
        self.assertEqual(self.levels(original.CAPTURE_SINK), [40000] * 6)
        self.assertTrue(self.sink(original.CAPTURE_SINK)["mute"])
        self.assertFalse(any(call[2] == "speaker" for call in self.masters()))
        self.assertEqual(self.audio.default, original.CAPTURE_SINK)
        self.assertEqual(self.audio.streams[0]["sink"], 99)

    def test_physical_volume_keys_update_only_virtual_master(self):
        self.activate()
        self.sink()["volume"] = self.audio.volume([45000, 41000])
        self.assertTrue(original._sync_master_locked())
        self.assertEqual(self.levels(), [45000, 41000])
        self.assertEqual(self.levels(original.CAPTURE_SINK), self.surround(45000, 41000))
        self.assertEqual([call[2] for call in self.masters()], [original.CAPTURE_SINK])

    def test_virtual_volume_ui_updates_physical_with_absolute_stereo_values(self):
        self.activate()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(
            self.surround(45000, 41000), surround=True)
        self.assertTrue(original._sync_master_locked())
        self.assertEqual(self.levels(), [45000, 41000])
        self.assertEqual(self.masters(), [("pactl", "set-sink-volume", "speaker", "45000", "41000")])
        self.assertEqual(read_json(original.ROUTING_FILE)["master_mirror"]["last_direction"], "virtual")

    def test_simultaneous_volume_conflict_preserves_physical_level(self):
        self.activate()
        self.sink()["volume"] = self.audio.volume(50000)
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        original._sync_master_locked()
        self.assertEqual(self.levels(), [50000] * 2)
        self.assertEqual(self.levels(original.CAPTURE_SINK), [50000] * 6)
        self.assertFalse(any(call[2] == "speaker" for call in self.masters()))

    def test_physical_volume_and_virtual_mute_merge_independently(self):
        self.activate()
        self.sink()["volume"] = self.audio.volume(50000)
        self.sink(original.CAPTURE_SINK)["mute"] = True
        original._sync_master_locked()
        self.assertEqual(self.levels(), [50000] * 2)
        self.assertEqual(self.levels(original.CAPTURE_SINK), [50000] * 6)
        self.assertTrue(self.sink()["mute"])
        self.assertTrue(self.sink(original.CAPTURE_SINK)["mute"])
        self.assertNotIn(("pactl", "set-sink-volume", "speaker", "50000", "50000"), self.masters())

    def test_virtual_volume_and_physical_mute_merge_independently(self):
        self.activate()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        self.sink()["mute"] = True
        original._sync_master_locked()
        self.assertEqual(self.levels(), [45000] * 2)
        self.assertTrue(self.sink(original.CAPTURE_SINK)["mute"])
        self.assertFalse(any(call[1:3] == ("set-sink-mute", "speaker") for call in self.masters()))

    def test_virtual_unmute_is_forwarded_to_physical(self):
        self.activate(mute=True)
        self.sink(original.CAPTURE_SINK)["mute"] = False
        original._sync_master_locked()
        self.assertFalse(self.sink()["mute"])
        self.assertEqual(self.masters(), [("pactl", "set-sink-mute", "speaker", "0")])

    def test_stable_polling_never_rewrites_audio_or_recovery_journal(self):
        self.activate(volume=[30000, 40000])
        with patch.object(original, "write_json") as write:
            for _ in range(3):
                self.assertTrue(original._sync_master_locked())
            write.assert_not_called()
        self.assertEqual(self.audio.mutations(), [])

    def test_rounded_readback_becomes_stable_baseline_without_repeated_writes(self):
        self.activate()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        def rounded(args, **kwargs):
            result = self.audio.run(args, **kwargs)
            if args[1:3] == ["set-sink-volume", "speaker"]:
                self.sink()["volume"] = self.audio.volume(44998)
            elif args[1:3] == ["set-sink-volume", original.CAPTURE_SINK]:
                self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(44997, surround=True)
            return result
        with patch.object(original, "run", side_effect=rounded):
            original._sync_master_locked()
            self.assertEqual(self.levels(), [44998] * 2)
            self.assertEqual(self.levels(original.CAPTURE_SINK), [44997] * 6)
            journal = read_json(original.ROUTING_FILE)
            self.assertEqual(journal["master_mirror"]["last_physical"]["volume"], [44998] * 2)
            self.assertEqual(journal["master_mirror"]["last_virtual"]["volume"], [44997] * 6)
            self.audio.calls.clear()
            with patch.object(original, "write_json") as write:
                original._sync_master_locked()
                original._sync_master_locked()
                write.assert_not_called()
        self.assertEqual(self.audio.mutations(), [])

    def test_sync_ignores_unrouted_restored_and_legacy_journals(self):
        self.activate()
        record = read_json(original.ROUTING_FILE)
        for overrides in ({"routed": False}, {"restored": True}, {"schema": "ally-dsp-original-routing-v1"}):
            with self.subTest(overrides=overrides):
                write_json(original.ROUTING_FILE, {**record, **overrides})
                self.audio.calls.clear()
                self.assertFalse(original._sync_master_locked())
                self.assertEqual(self.audio.calls, [])

    def test_foreign_virtual_token_or_module_cannot_receive_master_updates(self):
        self.activate()
        for field in ("token", "module"):
            with self.subTest(field=field):
                owned = self.sink(original.CAPTURE_SINK)
                before = copy.deepcopy(owned)
                if field == "token":
                    owned["properties"]["allydsp.session"] = "foreign"
                else:
                    owned["owner_module"] = 777
                with self.assertRaisesRegex(RuntimeError, "ownership changed"):
                    original._sync_master_locked()
                self.assertEqual(self.audio.mutations(), [])
                owned.clear()
                owned.update(before)

    def test_replacement_physical_index_or_serial_is_never_changed(self):
        self.activate()
        for field, value in (("index", 123), ("serial", "replacement")):
            with self.subTest(field=field):
                physical = self.sink()
                before = copy.deepcopy(physical)
                if field == "serial":
                    physical["properties"]["object.serial"] = value
                else:
                    physical[field] = value
                self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
                with self.assertRaisesRegex(RuntimeError, "ownership changed"):
                    original._sync_master_locked()
                self.assertEqual(self.audio.mutations(), [])
                physical.clear()
                physical.update(before)

    def test_restore_flushes_unsynced_virtual_value_then_preserves_that_current_physical_level(self):
        self.activate()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        self.sink(original.CAPTURE_SINK)["mute"] = True
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [45000] * 2)
        self.assertTrue(self.sink()["mute"])
        self.assertEqual(self.audio.default, "speaker")
        self.assertEqual(self.audio.streams[0]["sink"], 10)
        self.assertTrue(read_json(original.ROUTING_FILE)["restored"])
        self.assertFalse(any(sink["name"] == original.CAPTURE_SINK for sink in self.audio.sinks))
        self.assertNotIn(("pactl", "set-sink-volume", "speaker", "34734", "34734"), self.masters())

    def test_restore_keeps_new_external_physical_level_instead_of_old_virtual_snapshot(self):
        self.activate()
        self.sink()["volume"] = self.audio.volume(50000)
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [50000] * 2)
        self.assertFalse(any(call[2] == "speaker" for call in self.masters()))

    def test_restore_with_missing_virtual_sink_leaves_current_physical_untouched(self):
        self.activate()
        self.audio.sinks = [sink for sink in self.audio.sinks if sink["name"] != original.CAPTURE_SINK]
        self.audio.default = "speaker"
        self.audio.streams = []
        self.sink()["volume"] = self.audio.volume(41000)
        self.sink()["mute"] = True
        original._restore_routing_locked()
        self.assertEqual(self.levels(), [41000] * 2)
        self.assertTrue(self.sink()["mute"])
        self.assertEqual(self.masters(), [])
        self.assertTrue(read_json(original.ROUTING_FILE)["restored"])

    def test_restore_records_failed_final_sync_but_still_recovers_routes(self):
        self.activate()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        def fail_volume(args, **kwargs):
            if args[1:3] == ["set-sink-volume", "speaker"]:
                return Result(1, "", "physical volume write failed")
            return self.audio.run(args, **kwargs)
        with patch.object(original, "run", side_effect=fail_volume):
            original._restore_routing_locked()
        self.assertEqual(self.levels(), [34734] * 2)
        self.assertEqual(self.audio.default, "speaker")
        self.assertEqual(self.audio.streams[0]["sink"], 10)
        journal = read_json(original.ROUTING_FILE)
        self.assertTrue(journal["restored"])
        self.assertIn("physical volume write failed", journal["master_sync_recovery_error"])

    def test_sync_persists_each_intent_before_mutating_either_master(self):
        self.activate()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        self.sink()["mute"] = True
        observed = []
        def inspect(args, **kwargs):
            if args[1] in ("set-sink-volume", "set-sink-mute"):
                control = "volume" if args[1] == "set-sink-volume" else "mute"
                target = "physical" if args[2] == "speaker" else "virtual"
                value = list(map(int, args[3:])) if control == "volume" else args[3] == "1"
                intent = read_json(original.ROUTING_FILE)["master_mirror"]["intent"]
                self.assertEqual(intent, {"control": control, "target": target, "value": value})
                observed.append((control, target))
            return self.audio.run(args, **kwargs)
        with patch.object(original, "run", side_effect=inspect):
            original._sync_master_locked()
        self.assertEqual(observed, [("volume", "physical"), ("mute", "virtual")])
        self.assertIsNone(read_json(original.ROUTING_FILE)["master_mirror"]["intent"])

    def test_failed_intent_persistence_cannot_change_physical_master(self):
        self.activate()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        with patch.object(original, "write_json", side_effect=OSError("journal full")):
            with self.assertRaisesRegex(OSError, "journal full"):
                original._sync_master_locked()
        self.assertEqual(self.levels(), [34734] * 2)
        self.assertEqual(self.audio.mutations(), [])

    def test_failed_physical_command_keeps_old_snapshot_and_retries_safely(self):
        self.activate()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        def fail_volume(args, **kwargs):
            if args[1:3] == ["set-sink-volume", "speaker"]:
                return Result(1, "", "write failed")
            return self.audio.run(args, **kwargs)
        with patch.object(original, "run", side_effect=fail_volume):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                original._sync_master_locked()
        mirror = read_json(original.ROUTING_FILE)["master_mirror"]
        self.assertEqual(mirror["last_physical"]["volume"], [34734] * 2)
        self.assertEqual(mirror["intent"]["target"], "physical")
        original._sync_master_locked()
        self.assertEqual(self.levels(), [45000] * 2)
        self.assertIsNone(read_json(original.ROUTING_FILE)["master_mirror"]["intent"])

    def test_crash_after_physical_write_retries_without_replaying_a_stale_level(self):
        self.activate()
        self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
        def crash_on_commit(path, value):
            if path == original.ROUTING_FILE and value["master_mirror"]["intent"] is None:
                raise OSError("crash after physical command")
            return write_json(path, value)
        with patch.object(original, "write_json", side_effect=crash_on_commit):
            with self.assertRaisesRegex(OSError, "crash after physical command"):
                original._sync_master_locked()
        self.assertEqual(self.levels(), [45000] * 2)
        self.assertEqual(read_json(original.ROUTING_FILE)["master_mirror"]["intent"]["target"], "physical")
        self.sink()["volume"] = self.audio.volume(50000)
        self.audio.calls.clear()
        original._sync_master_locked()
        self.assertEqual(self.levels(), [50000] * 2)
        self.assertEqual(self.levels(original.CAPTURE_SINK), [50000] * 6)
        self.assertFalse(any(call[2] == "speaker" for call in self.masters()))

    def test_pending_mute_is_not_discarded_by_an_unchanged_volume_control(self):
        self.activate()
        record = read_json(original.ROUTING_FILE)
        record["master_mirror"]["intent"] = {"control": "mute", "target": "physical", "value": True}
        write_json(original.ROUTING_FILE, record)
        self.sink(original.CAPTURE_SINK)["mute"] = True
        original._sync_master_locked()
        self.assertTrue(self.sink()["mute"])
        self.assertEqual(self.masters(), [("pactl", "set-sink-mute", "speaker", "1")])
        self.assertIsNone(read_json(original.ROUTING_FILE)["master_mirror"]["intent"])

    def test_intervening_virtual_volume_change_is_not_committed_as_successful_mirror(self):
        self.activate()
        previous = copy.deepcopy(read_json(original.ROUTING_FILE)["master_mirror"])
        self.sink()["volume"] = self.audio.volume(50000)
        def competing_ui(args, **kwargs):
            result = self.audio.run(args, **kwargs)
            if args[1:3] == ["set-sink-volume", original.CAPTURE_SINK]:
                self.sink(original.CAPTURE_SINK)["volume"] = self.audio.volume(45000, surround=True)
            return result
        with patch.object(original, "run", side_effect=competing_ui):
            with self.assertRaisesRegex(RuntimeError, "virtual master changed"):
                original._sync_master_locked()
        pending = read_json(original.ROUTING_FILE)["master_mirror"]
        self.assertEqual(pending["last_physical"], previous["last_physical"])
        self.assertEqual(pending["last_virtual"], previous["last_virtual"])
        self.assertEqual(pending["intent"], {"control": "volume", "target": "virtual", "value": [50000] * 6})
        original._sync_master_locked()
        self.assertEqual(self.levels(), [50000] * 2)
        self.assertEqual(self.levels(original.CAPTURE_SINK), [50000] * 6)
        self.assertIsNone(read_json(original.ROUTING_FILE)["master_mirror"]["intent"])

    def test_intervening_virtual_mute_change_retains_previous_baseline_for_retry(self):
        self.activate()
        self.sink()["mute"] = True
        def competing_ui(args, **kwargs):
            result = self.audio.run(args, **kwargs)
            if args[1:3] == ["set-sink-mute", original.CAPTURE_SINK]:
                self.sink(original.CAPTURE_SINK)["mute"] = False
            return result
        with patch.object(original, "run", side_effect=competing_ui):
            with self.assertRaisesRegex(RuntimeError, "virtual master changed"):
                original._sync_master_locked()
        pending = read_json(original.ROUTING_FILE)["master_mirror"]
        self.assertFalse(pending["last_physical"]["mute"])
        self.assertFalse(pending["last_virtual"]["mute"])
        self.assertEqual(pending["intent"], {"control": "mute", "target": "virtual", "value": True})
        original._sync_master_locked()
        self.assertTrue(self.sink()["mute"])
        self.assertTrue(self.sink(original.CAPTURE_SINK)["mute"])

    def test_new_physical_volume_during_mute_write_is_observed_on_the_next_poll(self):
        self.activate()
        self.sink(original.CAPTURE_SINK)["mute"] = True
        def new_volume(args, **kwargs):
            result = self.audio.run(args, **kwargs)
            if args[1:3] == ["set-sink-mute", "speaker"]:
                self.sink()["volume"] = self.audio.volume(50000)
            return result
        with patch.object(original, "run", side_effect=new_volume):
            original._sync_master_locked()
        self.assertEqual(read_json(original.ROUTING_FILE)["master_mirror"]["last_physical"]["volume"], [34734] * 2)
        original._sync_master_locked()
        self.assertEqual(self.levels(), [50000] * 2)
        self.assertEqual(self.levels(original.CAPTURE_SINK), [50000] * 6)

    def test_public_sync_master_requires_the_actual_production_entry_point(self):
        # The watcher calls this name. Do not replace it with a create=True
        # mock or call the locked helper directly: either hides a missing API.
        self.assertTrue(callable(original.sync_master))

    def test_public_sync_master_routes_a_verified_live_v2_session_through_the_locked_helper(self):
        self.activate()
        with patch.object(original, "verify", return_value=True) as verify, \
             patch.object(original, "_routing_action") as action:
            self.assertTrue(original.sync_master())
        verify.assert_called_once_with(0)
        action.assert_called_once_with("sync")
        self.assertEqual(self.audio.mutations(), [])

    def test_public_sync_master_ignores_invalid_unrouted_restored_and_legacy_records(self):
        self.activate()
        record = read_json(original.ROUTING_FILE)
        records = [None, [], {}, {**record, "routed": False},
                   {**record, "restored": True}, {**record, "schema": "ally-dsp-original-routing-v1"}]
        for value in records:
            with self.subTest(record=value):
                write_json(original.ROUTING_FILE, value)
                with patch.object(original, "verify") as verify, \
                     patch.object(original, "_routing_action") as action:
                    self.assertFalse(original.sync_master())
                action.assert_not_called()
                verify.assert_not_called()
        self.assertEqual(self.audio.mutations(), [])

    def test_public_sync_master_ignores_unverified_worker_without_launching_the_helper(self):
        self.activate()
        with patch.object(original, "verify", return_value=False) as verify, \
             patch.object(original, "_routing_action") as action:
            self.assertFalse(original.sync_master())
        verify.assert_called_once_with(0)
        action.assert_not_called()
        self.assertEqual(self.audio.mutations(), [])

    def test_public_sync_master_does_not_report_success_after_a_helper_failure(self):
        self.activate()
        with patch.object(original, "verify", return_value=True), \
             patch.object(original, "_routing_action", side_effect=RuntimeError("sync helper failed")):
            with self.assertRaisesRegex(RuntimeError, "sync helper failed"):
                original.sync_master()
        self.assertEqual(self.audio.mutations(), [])


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


class OutputGainTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="output-gain-test-", dir=ROOT / "tests")))
        for name in ("ROUTING_FILE", "META_FILE", "STATUS_FILE"):
            self.stack.enter_context(patch.object(original, name, str(self.root / (name + ".json"))))
        self.audio = AudioSession()
        self.audio.streams.append({"index": 101, "sink": 10, "volume_raw": 65536,
                                   "properties": {"node.name": original.NAMESPACE + ".playback"}})
        self.stack.enter_context(patch.object(original, "run", self.audio.run))
        self.real_verify = original.verify
        self.verify = self.stack.enter_context(patch.object(original, "verify", return_value=True))
        self.stack.enter_context(patch.object(original.settings, "load", return_value={"original": {"outputGainDb": 6}}))
        write_json(original.ROUTING_FILE, {"physical_sink": "speaker"})
        write_json(original.META_FILE, {"engine": "original", "profile": "game", "outputGainDb": 0})

    def test_gain_accepts_finite_inclusive_zero_to_twelve_db(self):
        for value in (0, 6, 12, "6"):
            with self.subTest(value=value):
                self.assertEqual(original.validate_output_gain(value), float(value))

    def test_out_of_range_and_nonfinite_gain_rejected_before_audio_commands(self):
        for value in (-0.01, 12.01, float("nan"), float("inf"), -float("inf"), "nan", "inf"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "between 0 and 12"):
                    original.apply_output_gain(value)
        self.assertEqual(self.audio.calls, [])

    def test_saved_gain_is_loaded_and_unspecified_setting_defaults_to_unity(self):
        self.assertEqual(original.output_gain_db(), 6)
        with patch.object(original.settings, "load", return_value={}):
            self.assertEqual(original.output_gain_db(), 0)

    def test_inactive_worker_keeps_all_audio_and_meta_unchanged(self):
        self.verify.return_value = False
        previous = Path(original.META_FILE).read_bytes()
        self.assertEqual(original.apply_output_gain(12), {"outputGainDb": 12, "applied": False})
        self.assertEqual(self.audio.calls, [])
        self.assertEqual(Path(original.META_FILE).read_bytes(), previous)

    def test_stale_ready_daemon_does_not_allow_any_pulse_volume_command(self):
        write_json(original.STATUS_FILE, {"ready": True, "state": "running", "daemon_pid": 111})
        # Use the real PID verification instead of treating a stale READY as active.
        with patch.object(original, "verify", self.real_verify), \
             patch.object(original.converted_runtime, "is_active", return_value=True), \
             patch.object(original.converted_runtime, "systemctl", return_value=Result(0, "999\n", "")):
            self.assertFalse(original.apply_output_gain(6)["applied"])
        self.assertEqual(self.audio.calls, [])

    def test_unrelated_game_stream_is_not_treated_as_owned_playback(self):
        self.audio.streams[1]["properties"]["node.name"] = "game.playback"
        with self.assertRaisesRegex(RuntimeError, "owned original playback"):
            original.apply_output_gain(6)
        self.assertEqual(self.audio.mutations(), [])

    def test_duplicate_playback_names_fail_without_choosing_one_stream(self):
        duplicate = copy.deepcopy(self.audio.streams[1])
        duplicate["index"] = 102
        self.audio.streams.append(duplicate)
        with self.assertRaisesRegex(RuntimeError, "owned original playback"):
            original.apply_output_gain(6)
        self.assertEqual(self.audio.mutations(), [])

    def test_playback_on_a_different_physical_sink_is_not_changed(self):
        self.audio.streams[1]["sink"] = 20
        with self.assertRaisesRegex(RuntimeError, "owned original playback"):
            original.apply_output_gain(6)
        self.assertEqual(self.audio.mutations(), [])

    def test_missing_journal_physical_sink_is_not_substituted_with_another_output(self):
        write_json(original.ROUTING_FILE, {"physical_sink": "missing"})
        with self.assertRaisesRegex(RuntimeError, "owned original playback"):
            original.apply_output_gain(6)
        self.assertEqual(self.audio.mutations(), [])

    def test_six_db_is_absolute_and_repeated_calls_do_not_accumulate_or_touch_other_volumes(self):
        game = copy.deepcopy(self.audio.streams[0])
        sinks = copy.deepcopy(self.audio.sinks)
        self.audio.streams[1]["volume_raw"] = 90000
        for _ in range(2):
            self.assertEqual(original.apply_output_gain(), {"outputGainDb": 6, "applied": True})
        self.assertEqual(self.audio.mutations(), [("pactl", "set-sink-input-volume", "101", "82505")] * 2)
        self.assertEqual(self.audio.streams[1]["volume_raw"], 82505)
        self.assertEqual(self.audio.streams[0], game)
        self.assertEqual(self.audio.sinks, sinks)
        self.assertEqual(self.audio.default, "speaker")
        self.assertEqual(read_json(original.META_FILE)["outputGainDb"], 6)

    def test_zero_restores_unity_and_twelve_sets_the_bounded_maximum(self):
        for value, raw in [(12, 103868), (0, 65536)]:
            with self.subTest(value=value):
                self.assertTrue(original.apply_output_gain(value)["applied"])
                self.assertEqual(self.audio.streams[1]["volume_raw"], raw)
                self.assertEqual(read_json(original.META_FILE)["outputGainDb"], value)

    def test_failed_pulse_volume_write_does_not_claim_changed_active_meta(self):
        previous = Path(original.META_FILE).read_bytes()
        def run(args, **kwargs):
            if args[1] == "set-sink-input-volume":
                return Result(1, "", "volume rejected")
            return self.audio.run(args, **kwargs)
        with patch.object(original, "run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "volume rejected"):
                original.apply_output_gain(6)
        self.assertEqual(Path(original.META_FILE).read_bytes(), previous)
        self.assertEqual(self.audio.streams[1]["volume_raw"], 65536)

    def test_successful_volume_write_is_not_rolled_back_for_auxiliary_metadata_failure(self):
        previous = Path(original.META_FILE).read_bytes()
        with patch.object(original, "write_json", side_effect=OSError("metadata storage unavailable")), \
             patch.object(original.logger, "warning") as warning:
            self.assertEqual(original.apply_output_gain(6), {"outputGainDb": 6, "applied": True})
        warning.assert_called_once()
        self.assertEqual(self.audio.streams[1]["volume_raw"], 82505)
        self.assertEqual(Path(original.META_FILE).read_bytes(), previous)


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

    def test_master_sync_uses_retained_helper_after_plugin_removal(self):
        original._ensure_recovery()
        self.remove_fixture_plugin()
        with patch.object(original, "run", return_value=Result(0, "", "")) as command, \
             patch.object(original, "user_env", side_effect=lambda value: value):
            original._routing_action("sync")
        args, kwargs = command.call_args
        self.assertEqual(args[0], ["/usr/bin/python3", original._recovery_script(), "--action", "sync"])
        self.assertEqual(kwargs["env"]["DECKY_PLUGIN_RUNTIME_DIR"], original.paths.RUNTIME_DIR)
        self.assertTrue(Path(args[0][1]).is_file())

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

    def set_gain(self, value):
        async def scenario():
            self.plugin.engine_lock = asyncio.Lock()
            return await self.plugin.set_original_gain(value)
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

    def test_gain_api_rejects_converted_engine_without_save_or_volume_change(self):
        previous = copy.deepcopy(self.plugin.settings)
        with patch.object(self.main.original_runtime, "apply_output_gain") as apply:
            with self.assertRaisesRegex(RuntimeError, "Select the original engine"):
                self.set_gain(6)
        apply.assert_not_called()
        self.assertEqual(self.plugin.settings, previous)
        self.assertEqual(self.events, [])

    def test_gain_api_rejects_nonfinite_request_without_save_or_volume_change(self):
        self.plugin.settings["engine"]["kind"] = "original"
        previous = copy.deepcopy(self.plugin.settings)
        with patch.object(self.main.original_runtime, "apply_output_gain") as apply:
            with self.assertRaisesRegex(ValueError, "between 0 and 12"):
                self.set_gain(float("nan"))
        apply.assert_not_called()
        self.assertEqual(self.plugin.settings, previous)
        self.assertEqual(self.events, [])

    def test_gain_api_saves_then_applies_under_shared_lock_and_preserves_other_settings(self):
        self.plugin.settings["engine"]["kind"] = "original"
        self.plugin.settings["original"] = {"outputGainDb": 3, "other": "preserve"}
        saved = []
        self.plugin._save = lambda: saved.append(copy.deepcopy(self.plugin.settings))
        def apply(value):
            self.assertTrue(self.plugin.engine_lock.locked())
            self.assertEqual(saved[-1]["original"]["outputGainDb"], value)
            return {"outputGainDb": value, "applied": True}
        with patch.object(self.main.original_runtime, "apply_output_gain", side_effect=apply) as command:
            result = self.set_gain(6)
        command.assert_called_once_with(6)
        self.assertEqual(result, {"ok": True, "outputGainDb": 6, "applied": True})
        self.assertEqual(self.plugin.settings["original"], {"outputGainDb": 6, "other": "preserve"})
        self.assertEqual(len(saved), 1)

    def test_gain_api_saves_for_next_start_when_worker_is_inactive(self):
        self.plugin.settings["engine"]["kind"] = "original"
        with patch.object(self.main.original_runtime, "apply_output_gain",
                          return_value={"outputGainDb": 12, "applied": False}) as apply:
            result = self.set_gain(12)
        apply.assert_called_once_with(12)
        self.assertEqual(self.plugin.settings["original"]["outputGainDb"], 12)
        self.assertEqual(result, {"ok": True, "outputGainDb": 12, "applied": False})
        self.assertEqual(self.events, ["save:original"])

    def test_gain_api_restores_and_saves_previous_settings_when_apply_fails(self):
        self.plugin.settings["engine"]["kind"] = "original"
        previous = {"outputGainDb": 3, "other": "preserve"}
        self.plugin.settings["original"] = copy.deepcopy(previous)
        saved = []
        self.plugin._save = lambda: saved.append(copy.deepcopy(self.plugin.settings["original"]))
        with patch.object(self.main.original_runtime, "apply_output_gain", side_effect=RuntimeError("owned stream missing")):
            with self.assertRaisesRegex(RuntimeError, "owned stream missing"):
                self.set_gain(6)
        self.assertEqual(saved, [{"outputGainDb": 6, "other": "preserve"}, previous])
        self.assertEqual(self.plugin.settings["original"], previous)

    def test_gain_api_first_save_failure_restores_memory_and_never_applies_volume(self):
        self.plugin.settings["engine"]["kind"] = "original"
        previous = {"outputGainDb": 3, "other": "preserve"}
        self.plugin.settings["original"] = copy.deepcopy(previous)
        def save():
            raise RuntimeError("settings storage unavailable")
        self.plugin._save = save
        with patch.object(self.main.original_runtime, "apply_output_gain") as apply:
            with self.assertRaisesRegex(RuntimeError, "settings storage unavailable"):
                self.set_gain(6)
        apply.assert_not_called()
        self.assertEqual(self.plugin.settings["original"], previous)


if __name__ == "__main__":
    unittest.main()
