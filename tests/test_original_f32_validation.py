"""IEEE edge cases and descriptor exclusion for the shared raw-plane checker."""
import array
import math
import struct

import pytest

from allydsp.original.protocol import is_finite_f32le
from allydsp.original import object_protocol as wire


def test_all_signed_exponents_and_mantissa_extremes_match_ieee_reference():
    for sign in (0, 1):
        for exponent in range(256):
            for mantissa in (0, 1, 0x400000, 0x7fffff):
                raw = struct.pack('<I', sign << 31 | exponent << 23 | mantissa)
                assert is_finite_f32le(raw) == math.isfinite(struct.unpack('<f', raw)[0])


@pytest.mark.parametrize('length', [0, 1, 256, 1024, 32768])
def test_finite_neighbors_signs_subnormals_and_maximums_do_not_cross_words(length):
    finite = (0, 0x80000000, 0x007fffff, 0x00800000, 0x7f7fffff, 0xff7fffff, 0x7f000000, 0xff000001)
    raw = b''.join(struct.pack('<I', finite[index % len(finite)]) for index in range(length))
    assert is_finite_f32le(raw) is True


@pytest.mark.parametrize('bits', [0x7f800000, 0xff800000, 0x7f800001, 0xff800001, 0x7fc00000, 0xffc00001])
@pytest.mark.parametrize('index', [0, 255, 256, 1023])
def test_nan_and_infinity_at_word_boundaries_are_rejected_without_mutation(bits, index):
    raw = bytearray(4096)
    struct.pack_into('<I', raw, index * 4, bits)
    before = bytes(raw)
    assert is_finite_f32le(raw) is False
    assert bytes(raw) == before


@pytest.mark.parametrize('length', [1, 2, 3, 5, 4097])
def test_incomplete_float_words_are_explicitly_rejected(length):
    with pytest.raises(ValueError, match='complete little-endian float32'):
        is_finite_f32le(bytes(length))


def test_decoder_never_treats_integer_descriptor_bits_as_float_samples():
    item = {'id': 0x7f800001, 'type': 1, 'position': (0, 0, -1), 'gain': 1,
            'samples': array.array('f', [0] * 1024)}
    payload = wire.encode_objects([item], 1024)
    assert wire.decode_batch(payload, 1024, 1)[0]['id'] == item['id']


@pytest.mark.parametrize('frames', [256, 1024])
def test_decoder_checks_complete_plane_before_dispatch_and_keeps_framing_guards(frames):
    item = {'id': 1, 'type': 1, 'position': (0, 0, -1), 'gain': 1,
            'samples': array.array('f', [0] * frames)}
    payload = bytearray(wire.encode_objects([item], frames))
    decode = wire.decode_objects if frames == 256 else wire.decode_batch
    struct.pack_into('<I', payload, wire.DESCRIPTOR.size + (frames - 1) * 4, 0x7f800001)
    with pytest.raises(ValueError, match='finite source mono plane'):
        decode(payload, frames, 1)
    with pytest.raises(ValueError, match='count/frames/payload'):
        decode(payload[:-1], frames, 1)
