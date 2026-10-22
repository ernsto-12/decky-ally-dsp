"""Object framing, DSP dispatch and disable/reset contracts without vendor data."""
import array
import ctypes
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

import pytest

ORIGINAL = Path(__file__).resolve().parents[1] / 'py_modules/allydsp/original'


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ORIGINAL))
    loaded = {}
    for name in ('protocol', 'runtime_controls', 'windows_worker', 'object_protocol', 'object_engine', 'object_worker'):
        spec = importlib.util.spec_from_file_location(name, ORIGINAL / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        loaded[name] = module
    return SimpleNamespace(**loaded)


def source(identifier=1, kind=1, x=0.0, gain=1.0, value=0.25):
    return {'id': identifier, 'type': kind, 'position': (x, 0.0, -1.0),
            'gain': gain, 'samples': array.array('f', [value] * 256)}


def profile():
    return {'route': 'speakers', 'preset': 'game', 'voicing': 'balanced', 'fields': {
        '_band-frequencies': list(range(100, 2100, 100)),
        'graphic-equalizer-enable': False, 'graphic-equalizer-bands': [0] * 20,
        'output-mode/processing_mode': 11,
    }}


class OriginalEngine:
    def __init__(self, namespace, *, object_mapper):
        assert object_mapper is True
        self.metadata = {'engine': 'test-original', 'runtime_controls': True}
        self.control_profile = profile()
        self.events = []
        self.closed = False

    def apply_controls(self, fields):
        self.events.append(('controls', fields))
        return {'notification_verified': True, 'same_dsp': True, 'same_module': True}

    def process_objects(self, objects):
        self.events.append(('objects', objects))
        return array.array('f', [0.125] * 512).tobytes()

    def close(self):
        self.events.append(('close',))
        self.closed = True
        return {'same_dsp_instance': True, 'guards_unchanged': True}


def batch(modules, bypass=False):
    ns = SimpleNamespace(engine='hrtf-speakers', profile_catalog='generated.fixture', object_bypass=bypass)
    return modules.object_engine.ObjectBatchEngine(ns, factory=OriginalEngine)


def test_wire_roundtrip_preserves_sources_but_beta_budget_is_not_internal_capacity(modules):
    p = modules.object_protocol
    sources = [source(i + 1, x=i / 32) for i in range(32)]
    encoded = p.encode_objects(sources)
    decoded = p.decode_objects(encoded, 256, 32)
    assert decoded == sources
    assert p.INTERNAL_DYNAMIC_CAPACITY == 128 and p.INTERNAL_SOURCE_CAPACITY == 145
    assert p.BETA_SOURCE_BUDGET == p.MAX_OBJECTS == 32
    with pytest.raises(ValueError, match='beta budget'):
        p.encode_objects(sources + [source(33)])
    with pytest.raises(ValueError, match='beta format'):
        p.decode_objects(encoded + encoded[:p.RECORD_BYTES], 256, 33)
    with pytest.raises(ValueError, match='magic'):
        modules.protocol.decode_header(p.packet(p.PROCESS, 1, 256, 32, encoded)[:p.HEADER.size])


@pytest.mark.parametrize('change', [
    {'id': 0}, {'id': True}, {'type': 3}, {'gain': 1.01}, {'gain': float('nan')},
    {'position': (float('inf'), 0, 0)}, {'samples': [float('nan')] * 256}, {'samples': [0] * 255},
    {'samples': [1e40] * 256},
])
def test_invalid_object_rejected_before_dsp_dispatch(modules, change):
    p = modules.object_protocol
    with pytest.raises((ValueError, TypeError, OverflowError)):
        p.encode_objects([dict(source(), **change)])


def test_duplicate_id_and_duplicate_static_channel_rejected(modules):
    p = modules.object_protocol
    for sources in ([source(), source()], [source(1, 16), source(2, 16)]):
        with pytest.raises(ValueError, match='uplicate'):
            p.encode_objects(sources)


def test_zero_source_block_still_dispatches_one_original_encode(modules):
    engine = batch(modules)
    assert len(engine.process(b'', 256, 0)) == 2048
    assert engine.engine.events == [('objects', [])]
    assert engine.close()['original_object_blocks'] == 1


def test_bypass_audio_never_calls_original_and_keeps_stereo_fl_fr(modules):
    engine = batch(modules, bypass=True)
    p = modules.object_protocol
    sources = [source(1, 2, value=0.2), source(2, 4, value=-0.3)]
    output = engine.process(p.encode_objects(sources), 256, 2)
    values = array.array('f'); values.frombytes(output)
    assert values[::2] == sources[0]['samples']
    assert values[1::2] == sources[1]['samples']
    assert engine.engine.events == []
    receipt = engine.configure({'schema': 'ally-dsp-original-controls-v1', 'controls': {}, 'bypass': False})
    assert receipt['audio_dolby_calls_when_bypassed'] == 0
    engine.process(p.encode_objects(sources), 256, 2)
    assert [event[0] for event in engine.engine.events] == ['controls', 'objects']
    assert engine.close()['bypass_blocks'] == 1


def test_fallback_is_finite_bounded_and_dynamic_position_affects_pan(modules):
    p, mixer = modules.object_protocol, modules.object_engine.bypass_stereo
    def levels(x):
        values = array.array('f'); values.frombytes(mixer([source(x=x, value=0.5)]))
        return values[0], values[1]
    assert levels(-1)[0] > levels(-1)[1]
    assert levels(1)[1] > levels(1)[0]
    out = array.array('f'); out.frombytes(mixer([source(i + 1, value=3e38) for i in range(32)]))
    assert max(out) == 1.0 and min(out) == 1.0


def test_official_control_reset_replays_full_profile_and_process_reset_requires_replacement(modules):
    engine = batch(modules)
    cut = engine.configure({'schema': 'ally-dsp-original-controls-v1',
                           'controls': {'eqEnabled': True, 'eqBandsDb': [-6] * 20}})
    assert cut['effective']['eqEnabled'] is True
    assert engine.engine.events[-1][1]['graphic-equalizer-bands'] == [-96] * 20
    reset = engine.configure({'schema': 'ally-dsp-original-controls-v1', 'controls': {}})
    assert reset['effective']['eqEnabled'] is False
    assert engine.engine.events[-1][1]['graphic-equalizer-bands'] == [0] * 20
    process_reset = engine.reset()
    assert process_reset['replacement_required'] is True
    assert process_reset['reset_complete'] is False
    assert process_reset['configuration']['controls'] == {}
    assert engine.closed and engine.engine.closed
    assert engine.engine.events[-1] == ('close',)


def test_bad_controls_and_nonbool_bypass_leave_current_configuration_unchanged(modules):
    engine = batch(modules)
    for request in ({'schema': 'bad', 'controls': {}},
                    {'schema': 'ally-dsp-original-controls-v1', 'controls': {}, 'bypass': 1},
                    {'schema': 'ally-dsp-original-controls-v1', 'controls': {'spatialEnabled': 'yes'}}):
        with pytest.raises(ValueError):
            engine.configure(request)
    assert engine.engine.events == [] and engine.controls == {} and engine.bypass is False


def responses(p, sink):
    stream = io.BytesIO(sink.getvalue())
    result = []
    while stream.tell() < len(stream.getvalue()):
        header = p.parse_header(stream.read(p.HEADER.size), response=True)
        result.append((header, stream.read(header['bytes'])))
    return result


def test_worker_interleaves_configuration_audio_and_clean_reset_receipt(modules):
    p, worker = modules.object_protocol, modules.object_worker
    engine = batch(modules)
    request = p.packet(p.CONFIGURE, 1, payload=p.json_bytes({
        'schema': 'ally-dsp-original-controls-v1', 'controls': {'dialogEnabled': True}, 'bypass': True}))
    request += p.packet(p.PROCESS, 2, 256, 0)
    request += p.packet(p.RESET, 3)
    sink = io.BytesIO()
    assert worker.serve(None, io.BytesIO(request), sink, factory=lambda _: engine) == 0
    result = responses(p, sink)
    assert [header['opcode'] for header, _ in result] == [p.READY, p.CONFIGURE, p.PROCESS, p.RESET]
    assert [header['sequence'] for header, _ in result] == [0, 1, 2, 3]
    assert json.loads(result[-1][1])['configuration']['bypass'] is True
    assert result[-2][1] == bytes(2048)
    assert engine.engine.events[-1] == ('close',)


@pytest.mark.parametrize('request_kind', ['sequence', 'frames', 'count', 'nonfinite', 'eof', 'reset-payload'])
def test_worker_rejects_invalid_transport_and_closes_original_once(modules, request_kind):
    p, worker = modules.object_protocol, modules.object_worker
    engine = batch(modules)
    if request_kind == 'sequence':
        request = p.packet(p.PROCESS, 2, 256, 0)
    elif request_kind == 'frames':
        request = p.packet(p.PROCESS, 1, 128, 0)
    elif request_kind == 'count':
        request = p.packet(p.PROCESS, 1, 256, 1)
    elif request_kind == 'nonfinite':
        raw = bytearray(p.encode_objects([source()])); struct.pack_into('<f', raw, p.DESCRIPTOR.size, float('nan'))
        request = p.packet(p.PROCESS, 1, 256, 1, raw)
    elif request_kind == 'reset-payload':
        request = p.packet(p.RESET, 1, payload=b'x')
    else:
        request = b''
    sink = io.BytesIO()
    assert worker.serve(None, io.BytesIO(request), sink, factory=lambda _: engine) == 1
    assert responses(p, sink)[-1][0]['opcode'] == p.ERROR
    assert engine.engine.events == [('close',)]


def test_original_object_batch_uses_verified_descriptor_flags_one_encode_and_stable_identity(modules, monkeypatch):
    # Exercise the actual frontend method with owned C buffers and stand-in
    # original function calls; no tuning values or private DLL memory writes.
    monkeypatch.setattr(ctypes, 'WINFUNCTYPE', ctypes.CFUNCTYPE, raising=False)
    spec = importlib.util.spec_from_file_location('_object_frontend_contract', ORIGINAL / 'frontend_adapter.py')
    frontend = importlib.util.module_from_spec(spec); spec.loader.exec_module(frontend)
    engine = frontend.HrtfFrontendAdapter.__new__(frontend.HrtfFrontendAdapter)
    engine.object_mapper = True
    engine.previous_objects = {}
    engine.mono, engine.stereo, engine.spec = frontend.Guarded(1024), frontend.Guarded(2048), frontend.Guarded(32)
    engine.engine_guard, engine.plugin_guard = frontend.Guarded(64), frontend.Guarded(64)
    engine.engine, engine.processing, engine.module, engine.dsp = 100, 200, 300, 400
    engine.frame_index, engine.timings = 0, []
    monkeypatch.setattr(frontend, 'getptr', lambda address: {200 + 0x538: 300, 300 + 0x60: 400}[address])
    events = []
    def setobject(_, descriptor, mono, frames, volume):
        events.append(('source', struct.unpack('<4I4f', ctypes.string_at(descriptor, 32))))
        assert frames == 256 and volume == 1
        return 0
    def encode(_, size, output, written):
        events.append(('encode',))
        ctypes.memset(output, 0, size)
        ctypes.cast(written, ctypes.POINTER(ctypes.c_uint32)).contents.value = size
        return 0
    engine.setobject, engine.process_original = setobject, encode
    assert engine.process_objects([source(7, 16), source(8, 1, x=-0.5)]) == bytes(2048)
    assert [event[0] for event in events] == ['source', 'source', 'encode']
    assert events[0][1][:4] == (7, 0, 16, 3)  # native LFE, no XYZ flag
    assert events[1][1][:4] == (8, 0, 1, 7)
    events.clear()
    engine.process_objects([source(8, 1, x=0.5)])
    assert events[0][1][:4] == (8, 0, 1, 6)  # existing ID, updated XYZ/gain
    assert len(engine.timings) == 2 and engine.frame_index == 2


@pytest.mark.parametrize('frames', [512, 1024])
def test_transport_batch_splits_into_exact_256_frame_original_calls_with_held_metadata(modules, frames):
    p = modules.object_protocol
    engine = batch(modules)
    samples = array.array('f', (index / frames for index in range(frames)))
    sources = [dict(source(7, x=-0.5), samples=samples), dict(source(8, 16), samples=samples)]
    encoded = p.encode_objects(sources, frames=frames)
    assert p.decode_batch(encoded, frames, 2) == sources
    output = engine.process_batch(encoded, frames, 2)
    assert len(output) == frames * 8
    calls = engine.engine.events
    assert len(calls) == frames // 256
    for index, (_, items) in enumerate(calls):
        assert items[0]['position'] == (-0.5, 0, -1) and items[1]['type'] == 16
        assert items[0]['samples'] == samples[index * 256:(index + 1) * 256]
        assert len(items[0]['samples']) == 256
    assert engine.close()['original_object_blocks'] == frames // 256


def test_batch_bounds_include_static_bed_and_bypass_still_never_calls_original(modules):
    p = modules.object_protocol
    sources = [dict(source(i + 1), samples=[0.1] * 1024) for i in range(32)]
    encoded = p.encode_objects(sources, frames=1024)
    assert len(encoded) == p.MAX_PAYLOAD == 32 * (24 + 4096)
    engine = batch(modules, bypass=True)
    assert len(engine.process_batch(encoded, 1024, 32)) == 8192
    assert engine.engine.events == []
    assert engine.close()['bypass_blocks'] == 4
    for frames, count, payload in ((256, 0, b''), (2048, 0, b''), (1024, 33, encoded), (1024, 32, encoded[:-4])):
        with pytest.raises(ValueError):
            p.decode_batch(payload, frames, count)


def test_worker_batch_configuration_and_single_process_share_ordered_sequence(modules):
    p, worker = modules.object_protocol, modules.object_worker
    engine = batch(modules)
    sources = [dict(source(), samples=[0.1] * 1024)]
    request = p.packet(p.BATCH, 1, 1024, 1, p.encode_objects(sources, frames=1024))
    request += p.packet(p.CONFIGURE, 2, payload=p.json_bytes({
        'schema': 'ally-dsp-original-controls-v1', 'controls': {}, 'bypass': True}))
    request += p.packet(p.BATCH, 3, 512, 0)
    request += p.packet(p.PROCESS, 4, 256, 0)
    request += p.packet(p.QUIT, 5)
    sink = io.BytesIO()
    assert worker.serve(None, io.BytesIO(request), sink, factory=lambda _: engine) == 0
    result = responses(p, sink)
    assert [header['opcode'] for header, _ in result] == [p.READY, p.BATCH, p.CONFIGURE, p.BATCH, p.PROCESS, p.QUIT]
    assert (result[1][0]['frames'], result[1][0]['count'], len(result[1][1])) == (1024, 2, 8192)
    assert result[3][1] == bytes(4096) and result[4][1] == bytes(2048)
    summary = json.loads(result[-1][1])
    assert summary['original_object_blocks'] == 4 and summary['bypass_blocks'] == 3
