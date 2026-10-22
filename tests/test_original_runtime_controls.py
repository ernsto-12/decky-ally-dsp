"""ASAR transport and user boundaries; no proprietary tuning or DLL fixtures."""
import hashlib
import importlib.util
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from allydsp.original.runtime_controls import (
    AO_KEYS, REGULATOR_KEYS, RUNTIME_SLOTS, controls_to_fields,
    encode_runtime, fields_to_controls, normalize_controls,
)


def slot_payload(fields, index, expected):
    blob = encode_runtime(fields)
    assert blob[:index] == bytes(index)
    assert blob[index:index + len(expected)] == expected
    assert blob[index + len(expected):] == bytes(len(RUNTIME_SLOTS) - index - 1)


def test_empty_is_only_absence_flags_and_explicit_false_is_present():
    assert encode_runtime({}) == bytes(len(RUNTIME_SLOTS))
    index = next(i for i, (_, name, _) in enumerate(RUNTIME_SLOTS) if name == 'graphic-equalizer-enable')
    slot_payload({'graphic-equalizer-enable': False}, index, b'\x01\x00')
    slot_payload({'graphic-equalizer-enable': True}, index, b'\x01\x01')


def test_signed_scalar_and_packed_frequency_pairs():
    slot_payload({'system-gain': -16}, 4, b'\x01' + struct.pack('<i', -16))
    index = next(i for i, (_, name, _) in enumerate(RUNTIME_SLOTS) if name == 'graphic-equalizer-bands')
    slot_payload({'graphic-equalizer-bands': [-96, 32], '_band-frequencies': [100, 1000]},
                 index, b'\x01' + struct.pack('<iiiii', 2, 100, -96, 1000, 32))


def test_ao_channel_presence_and_regulator_bool_width():
    index = next(i for i, (_, name, _) in enumerate(RUNTIME_SLOTS) if name == 'audio-optimizer-bands')
    payload = b'\x01' + struct.pack('<iii', 2, 100, 1000)
    payload += b'\x01' + struct.pack('<ii', 1, -1) + bytes(8) + b'\x01' + struct.pack('<ii', 2, -2)
    slot_payload({AO_KEYS[0]: [1, -1], AO_KEYS[9]: [2, -2], '_band-frequencies': [100, 1000]}, index, payload)
    index = next(i for i, (_, name, _) in enumerate(RUNTIME_SLOTS) if name == 'regulator-tuning')
    slot_payload({REGULATOR_KEYS[0]: [-16], REGULATOR_KEYS[1]: [0],
                  REGULATOR_KEYS[2]: [1], '_band-frequencies': [100]}, index,
                 b'\x01' + struct.pack('<iiii', 1, 100, -16, 0) + b'\x01')


@pytest.mark.parametrize('fields', [
    {'unknown': 0}, {'output-mode/output_ports': [1, 2]},
    {'graphic-equalizer-enable': 'true'}, {'graphic-equalizer-enable': 2},
    {'system-gain': float('nan')}, {'system-gain': 2**31}, {'system-gain': True},
    {'graphic-equalizer-bands': [0]},
    {'graphic-equalizer-bands': [0, 0], '_band-frequencies': [100]},
    {'graphic-equalizer-bands': [0, 0], '_band-frequencies': [100, 100]},
    {'graphic-equalizer-bands': [0, 0], '_band-frequencies': [1000, 100]},
    {'graphic-equalizer-bands': [0], '_band-frequencies': [0]},
    {'graphic-equalizer-bands': [0], '_band-frequencies': [20001]},
    {'regulator-tuning/threshold_low': [-16], '_band-frequencies': [100]},
    {'virtual-bass-subgains': [0, 0]}, {'virtual-bass-src-freqs': [0, 1, 2]},
    {'advanced-speaker-virtualizer-rendering-config': [0]*9},
])
def test_encoder_rejects_unknown_malformed_and_unbounded_input(fields):
    with pytest.raises(ValueError):
        encode_runtime(fields)


def test_partial_overrides_preserve_profile_and_reset_to_original():
    base = {'graphic-equalizer-enable': 0, 'graphic-equalizer-bands': [0]*20,
            'output-mode/processing_mode': 11, '_band-frequencies': list(range(100, 2100, 100))}
    assert controls_to_fields({}, base) == base
    changed = controls_to_fields({'eqEnabled': True, 'eqBandsDb': [-0.5, 1.0] + [0]*18}, base)
    assert changed['graphic-equalizer-bands'] == [-8, 16] + [0]*18
    assert changed['graphic-equalizer-enable'] is True
    assert base['graphic-equalizer-bands'] == [0]*20
    assert controls_to_fields({}, base) == base
    assert fields_to_controls(base) == {'eqEnabled': False, 'eqBandsDb': [0]*20, 'spatialEnabled': True}
    assert fields_to_controls({}) == {}


@pytest.mark.parametrize('route,mode', [('speakers', 11), ('headphones', 9)])
def test_spatial_control_changes_only_explicit_mode(route, mode):
    assert controls_to_fields({'spatialEnabled': True}, {}, route=route) == {'output-mode/processing_mode': mode}
    assert controls_to_fields({'spatialEnabled': False}, {}, route=route) == {'output-mode/processing_mode': 1}


@pytest.mark.parametrize('controls', [
    {'eqBandsDb': [0]*19}, {'eqBandsDb': [0]*19 + [float('inf')]},
    {'eqBandsDb': [0]*19 + [12.5]}, {'eqBandsDb': [0]*19 + [0.25]},
    {'eqBandsDb': [0]*19 + [True]}, {'eqEnabled': 'false'},
    {'dialogAmount': 17}, {'dialogAmount': -1}, {'ieqAmount': 16.5},
    {'volumeLevelerAmount': 11}, {'spatialEnabled': 'yes'}, {'unsupported': True},
])
def test_user_control_validation(controls):
    with pytest.raises(ValueError):
        normalize_controls(controls)


def test_apply_controls_requires_verified_original_receipt_and_retains_bounded_audit(monkeypatch):
    source = Path(__file__).resolve().parents[1] / 'py_modules/allydsp/original/hrtf_adapter.py'
    monkeypatch.syspath_prepend(str(source.parent))
    spec = importlib.util.spec_from_file_location('_controls_delivery_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sent = []

    def notify(blob):
        sent.append(blob)
        digest = hashlib.sha256(blob).hexdigest()
        return {'blob_sha256': digest, 'same_module': True, 'same_dsp': True,
                'callback_queries': [{'kind': 'blob_bytes', 'supplied': True, 'sha256': digest}]}

    engine = SimpleNamespace(notify=notify, engine_guard=SimpleNamespace(check=lambda: True),
                             plugin_guard=SimpleNamespace(check=lambda: True))
    stream = module.HrtfStreamAdapter.__new__(module.HrtfStreamAdapter)
    stream.closed, stream.control_updates, stream.last_control_audit, stream.metadata = False, 0, None, {}
    stream.delegate = SimpleNamespace(engine=engine)
    stream.controls_readback = lambda: {'eqEnabled': 1}
    fields = {'graphic-equalizer-enable': True}
    audit = stream.apply_controls(fields)
    assert sent == [encode_runtime(fields)]
    assert audit['same_dsp'] and audit['notification_verified']
    assert audit['readback']['eqEnabled'] == 1
    assert audit['blob_sha256'] == hashlib.sha256(sent[0]).hexdigest()
    stream.apply_controls({})
    assert stream.control_updates == 2 and stream.last_control_audit['update'] == 2
    assert 'last_control_audit' in stream.metadata
    engine.notify = lambda blob: dict(notify(blob), same_dsp=False)
    with pytest.raises(RuntimeError, match='retain the verified engine'):
        stream.apply_controls(fields)
    assert stream.control_updates == 2
    stream.closed = True
    with pytest.raises(RuntimeError, match='closed'):
        stream.apply_controls(fields)
