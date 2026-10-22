"""A control acknowledgement shares the ordered PCM stream, with no restart."""
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / 'py_modules/allydsp/original'


@pytest.fixture
def loaded(monkeypatch):
    monkeypatch.syspath_prepend(str(ORIGINAL))
    modules = {}
    for name in ('protocol', 'windows_worker'):
        spec = importlib.util.spec_from_file_location(name, ORIGINAL / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    return modules['protocol'], modules['windows_worker']


def run_worker(loaded, monkeypatch, requests):
    protocol, worker = loaded
    events = []

    class Engine:
        block_frames, input_channels, output_channels = 256, 6, 2
        metadata = {'runtime_controls': True}
        closed = False
        control_profile = {'route': 'speakers', 'preset': 'game', 'voicing': 'balanced', 'fields': {
            '_band-frequencies': list(range(100, 2100, 100)),
            'graphic-equalizer-enable': False, 'graphic-equalizer-bands': [0] * 20,
            'output-mode/processing_mode': 11,
        }}

        def apply_controls(self, fields):
            events.append(('controls', fields))
            return {'same_dsp': True, 'notification_verified': True}

        def process(self, payload):
            events.append(('process', payload))
            return bytes(2048)

        def close(self):
            self.closed = True
            events.append(('close',))
            return {'same_dsp_instance': True, 'guards_unchanged': True}

    engine = Engine()
    sink = io.BytesIO()
    monkeypatch.setattr(worker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(requests), fileno=lambda: 0))
    monkeypatch.setattr(worker.sys, 'stdout', SimpleNamespace(buffer=sink, fileno=lambda: 1))
    monkeypatch.setattr(worker.sys, 'platform', 'win32')
    monkeypatch.setattr(worker.sys, 'argv', ['worker', '--dll', 'fixture', '--property-store', 'fixture', '--input-channels', '6'])
    monkeypatch.setattr(worker.os, 'O_BINARY', 0, raising=False)
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(setmode=lambda *_: None))
    monkeypatch.setattr(worker, 'create_engine', lambda _: engine)
    rc = worker.main()
    raw = io.BytesIO(sink.getvalue())
    responses = []
    while raw.tell() < len(raw.getvalue()):
        header = protocol.decode_header(raw.read(protocol.HEADER.size), response=True)
        payload = raw.read(header['payload_bytes'])
        responses.append((header, payload))
    return rc, engine, events, responses


def test_eq_and_reset_are_applied_between_pcm_blocks_to_one_engine(loaded, monkeypatch):
    p, _ = loaded
    def control(sequence, values):
        return p.encode(p.CONFIGURE, sequence, payload=p.json_bytes({
            'schema': 'ally-dsp-original-controls-v1', 'controls': values}))
    pcm = bytes(256 * 6 * 4)
    request = (control(1, {'eqEnabled': True, 'eqBandsDb': [-6.0] * 20})
               + p.encode(p.PROCESS, 2, 256, 6, pcm)
               + control(3, {}) + p.encode(p.PROCESS, 4, 256, 6, pcm)
               + p.encode(p.QUIT, 5))
    rc, engine, events, responses = run_worker(loaded, monkeypatch, request)
    assert rc == 0 and engine.closed
    assert [event[0] for event in events] == ['controls', 'process', 'controls', 'process', 'close']
    assert events[0][1]['graphic-equalizer-enable'] is True
    assert events[0][1]['graphic-equalizer-bands'] == [-96] * 20
    assert events[2][1]['graphic-equalizer-enable'] is False
    assert events[2][1]['graphic-equalizer-bands'] == [0] * 20
    assert [h['sequence'] for h, _ in responses] == list(range(6))
    assert json.loads(responses[1][1])['applied'] is True
    assert json.loads(responses[3][1])['effective']['eqEnabled'] is False


@pytest.mark.parametrize('controls', [None, {'eqBandsDb': [float('nan')] * 20},
                                     {'eqEnabled': 'yes'}, {'unrecognized': True}])
def test_bad_controls_fail_before_the_original_receiver_is_called(loaded, monkeypatch, controls):
    p, _ = loaded
    raw = json.dumps({'schema': 'ally-dsp-original-controls-v1', 'controls': controls}).encode()
    request = p.encode(p.CONFIGURE, 1, payload=raw)
    rc, engine, events, responses = run_worker(loaded, monkeypatch, request)
    assert rc == 1 and engine.closed
    assert events == [('close',)]
    assert responses[-1][0]['opcode'] == p.ERROR
