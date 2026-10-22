"""Persistent process-local VLLDP160 candidate postprocessor, unmodified DLL.

No vendor DLL, OEM tuning value, credential or protected field is embedded.
The caller supplies a local private normal-property plan and its trusted hash.
This stage is optional research composition, not proof of a live Windows chain.
"""
from __future__ import annotations

import ctypes as C
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import threading
import time
import uuid

try:
    from .protocol import is_finite_f32le
except ImportError:
    from protocol import is_finite_f32le


DLL_SHA256 = "28990e8d5845b012020b52947ceab0cf37199ede1e8162cd7d7a03db580af82c"
STORE_SHA256 = "f5cfc1d90ce8c58ab70bf700a941645bed340ed4f54b31a41d4800473877c0b4"
FMTID = uuid.UUID("5510c7ab-dfc2-40d0-a98b-5f77f697005e").bytes_le
# Recovered native wrapper ABI for exactly DLL_SHA256, not interchangeable
# among VLLDP versions. All functions are normal original entry points.
ABI = {"constructor": 0x9C790, "initialize": 0x9D040,
       "update": 0x9D870, "process": 0x181B10, "destructor": 0x9CA60}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _regular(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a local regular asset file")
    return path.resolve()


def _properties(items):
    if not isinstance(items, list) or len(items) > 128:
        raise ValueError("Invalid private property list")
    result = {}
    for item in items:
        raw = bytes.fromhex(item["key_hex"])
        if len(raw) != 20 or raw[:16] != FMTID or raw in result:
            raise ValueError("Unexpected or repeated VLLDP property")
        pid = struct.unpack_from("<I", raw, 16)[0]
        if not 1 <= pid <= 43:
            raise ValueError("Property is outside the recovered VLLDP API")
        kind, value = item["kind"], item["value"]
        values = value if kind == "blob_i32" else [value]
        if kind not in ("i4", "ui4", "blob_i32") or not isinstance(values, list) or len(values) > 65536:
            raise ValueError("Unsupported property value type")
        low, high = (0, 0xFFFFFFFF) if kind == "ui4" else (-0x80000000, 0x7FFFFFFF)
        if any(type(v) is not int or not low <= v <= high for v in values):
            raise ValueError("Invalid integer property payload")
        result[raw] = item
    return result


class GuardedBuffer:
    def __init__(self, size):
        self.size = size
        self.storage = C.create_string_buffer(size + 160)
        self.pointer = (C.addressof(self.storage) + 64 + 31) & ~31
        self.sentinel = bytes([0xA7]) * 64
        C.memmove(self.pointer - 64, self.sentinel, 64)
        C.memmove(self.pointer + size, self.sentinel, 64)
        C.memset(self.pointer, 0, size)

    def check(self):
        if (C.string_at(self.pointer - 64, 64) != self.sentinel
                or C.string_at(self.pointer + self.size, 64) != self.sentinel):
            raise RuntimeError("An original VLLDP host allocation guard changed")


class VlldpStreamAdapter:
    """One original DSP instance for an ordered stream of 256 stereo frames."""
    block_frames, sample_rate, input_channels, output_channels = 256, 48000, 2, 2

    def __init__(self, *, dll, plan, plan_sha256, property_store, variant="game"):
        if C.sizeof(C.c_void_p) != 8 or not hasattr(C, "WINFUNCTYPE"):
            raise RuntimeError("This original ABI requires 64-bit Windows Python or Wine")
        if variant not in ("game", "baseline"):
            raise ValueError("Expected game or baseline configuration")
        self.paths = {"dll": _regular(dll), "plan": _regular(plan), "property_store": _regular(property_store)}
        if sha256(self.paths["dll"]) != DLL_SHA256 or sha256(self.paths["property_store"]) != STORE_SHA256:
            raise ValueError("Original VLLDP DLL or public property-store pin mismatch")
        if len(plan_sha256) != 64 or sha256(self.paths["plan"]) != plan_sha256:
            raise ValueError("Private VLLDP application-plan pin mismatch")
        configuration = json.loads(self.paths["plan"].read_text(encoding="utf-8-sig"))
        if configuration.get("dll_sha256") != DLL_SHA256:
            raise ValueError("The private plan targets a different VLLDP version")
        init_values = _properties(configuration["initialization_properties"])
        values = _properties(configuration["properties"] if variant == "game"
                             else configuration["baseline_commit_properties"])
        self._thread = threading.get_ident()
        self._lock = threading.Lock()
        self.closed = False
        self.blocks = self.processing_ns = self.max_processing_ns = 0
        started = time.perf_counter_ns()
        spec = importlib.util.spec_from_file_location("_vlldp_property_store", self.paths["property_store"])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._store_module = module
        self._init = module.LocalPropertyStore(init_values)
        self._settings = module.LocalPropertyStore(values)
        self._directory = None
        if hasattr(__import__("os"), "add_dll_directory"):
            import os
            self._directory = os.add_dll_directory(str(self.paths["dll"].parent))
        self.library = C.WinDLL(str(self.paths["dll"]))
        self.object = GuardedBuffer(0x168)
        self.info = GuardedBuffer(0x80)
        self.pool = GuardedBuffer(64 * 1024 * 1024)
        self.input = GuardedBuffer(2048)
        self.output = GuardedBuffer(2048)
        self._fn("constructor", C.c_void_p, C.c_void_p)(self.object.pointer)
        # This is the confirmed normal caller-owned ApoInfo input structure,
        # not a write to vendor DSP state or a synthesized backend selection.
        data = bytearray(0x80)
        struct.pack_into("<IIII", data, 0, self.sample_rate, 2, 2, 2)
        struct.pack_into("<Q", data, 0x10, self._init.pointer)
        struct.pack_into("<I", data, 0x18, 1)
        struct.pack_into("<QQQ", data, 0x20, self.pool.pointer, self.pool.size, self.pool.pointer)
        C.memmove(self.info.pointer, bytes(data), len(data))
        self._fn("initialize", None, C.c_void_p, C.c_void_p)(self.object.pointer, self.info.pointer)
        self.dsp = C.c_void_p.from_address(self.object.pointer + 0x158).value
        if not self.dsp or self._u32(0x3C) != self.block_frames:
            raise RuntimeError("Original VLLDP initialization or block negotiation failed")
        self._fn("update", None, C.c_void_p, C.c_void_p, C.c_void_p)(
            self.object.pointer, None, self._settings.pointer)
        self._render = self._fn("process", None, C.c_void_p, C.c_void_p, C.c_void_p)
        self._check()
        supplied = {record["key"] for record in self._settings.audit if record["supplied"]}
        if len(supplied) != len(values):
            raise RuntimeError("The original backend did not query every selected property")
        self.metadata = {
            "engine": "original-vlldp160-candidate-post", "variant": variant,
            "block_frames": self.block_frames, "sample_rate": self.sample_rate,
            "input_channels": 2, "output_channels": 2,
            "dll_sha256": DLL_SHA256, "plan_sha256": plan_sha256,
            "property_store_sha256": STORE_SHA256,
            "host_sha256": sha256(Path(__file__)), "persistent_dsp_instance": True,
            "declared_latency_frames": self._u32(0x38),
            "max_channels": self._u32(0xA8), "max_operations": self._u32(0x160),
            "selected_property_count": len(values), "delivered_property_count": len(supplied),
            "init_seconds": (time.perf_counter_ns() - started) / 1e9,
            "scope": "Separate normal VLLDP160 post-stage candidate. Live Windows version/order and equality to device speaker calibration are unverified.",
        }

    def _fn(self, name, returns, *arguments):
        return C.WINFUNCTYPE(returns, *arguments)(self.library._handle + ABI[name])

    def _u32(self, offset):
        return C.c_uint32.from_address(self.object.pointer + offset).value

    def _check(self):
        for buffer in (self.object, self.info, self.pool, self.input, self.output):
            buffer.check()
        if self._init.errors or self._settings.errors:
            raise RuntimeError("An original property-store callback failed")
        if C.c_void_p.from_address(self.object.pointer + 0x158).value != self.dsp:
            raise RuntimeError("The original VLLDP DSP instance changed")

    def process(self, payload):
        if self.closed or threading.get_ident() != self._thread:
            raise RuntimeError("An open VLLDP stream must stay on its owning thread")
        if len(payload) != 2048 or not is_finite_f32le(payload):
            raise ValueError("Expected 256 finite little-endian float32 stereo frames")
        with self._lock:
            self._check()
            C.memmove(self.input.pointer, payload, len(payload))
            C.memset(self.output.pointer, 0, 2048)
            start = time.perf_counter_ns()
            self._render(self.object.pointer, self.output.pointer, self.input.pointer)
            duration = time.perf_counter_ns() - start
            self._check()
            raw = C.string_at(self.output.pointer, 2048)
            if not is_finite_f32le(raw):
                raise RuntimeError("Nonfinite original VLLDP output")
            self.blocks += 1
            self.processing_ns += duration
            self.max_processing_ns = max(self.max_processing_ns, duration)
            return raw

    def audit(self):
        def redacted(records):
            return [{key: record[key] for key in ("key", "supplied", "name", "kind") if key in record}
                    for record in records]
        return {"initialization_queries": redacted(self._init.audit),
                "runtime_queries": redacted(self._settings.audit),
                "callback_errors": len(self._init.errors) + len(self._settings.errors)}

    def close(self):
        if self.closed:
            raise RuntimeError("Original VLLDP stream is already closed")
        if threading.get_ident() != self._thread:
            raise RuntimeError("Close VLLDP on its owning thread")
        with self._lock:
            self._check()
            summary = dict(self.metadata, processed_blocks=self.blocks,
                           same_dsp_instance=True, guards_unchanged=True,
                           processing_seconds=self.processing_ns / 1e9,
                           max_block_seconds=self.max_processing_ns / 1e9)
            self._fn("destructor", None, C.c_void_p)(self.object.pointer)
            for buffer in (self.object, self.info, self.pool, self.input, self.output):
                buffer.check()
            self.closed = True
            if self._directory is not None:
                self._directory.close()
            return summary
