"""Bounded source-object transport, separate from the PCM worker protocol.

The pinned renderer supports 128 dynamic slots internally. The realtime beta
admits 32 total sources, including the static bed; these are different limits.
Each source is a stable ID, native type, XYZ/gain and 256 mono float32 samples.
"""
import array
import json
import math
import struct
import sys

try:
    from .protocol import is_finite_f32le
except ImportError:
    from protocol import is_finite_f32le

HEADER = struct.Struct('<4sHHIIII')
DESCRIPTOR = struct.Struct('<II4f')
REQUEST_MAGIC, RESPONSE_MAGIC = b'AOBJ', b'AOBR'
VERSION = 1
READY, PROCESS, RESET, QUIT, CONFIGURE, BATCH, ERROR = 0, 1, 2, 3, 4, 5, 0xffff
BLOCK_FRAMES, SAMPLE_RATE, OUTPUT_CHANNELS = 256, 48000, 2
BATCH_FRAMES = (512, 1024)
INTERNAL_DYNAMIC_CAPACITY, INTERNAL_SOURCE_CAPACITY = 128, 145
BETA_SOURCE_BUDGET = MAX_OBJECTS = MAX_DYNAMIC_OBJECTS = 32
STATIC_OBJECT_MASK = 0x3fffe
RECORD_BYTES = DESCRIPTOR.size + BLOCK_FRAMES * 4
MAX_CONTROL_BYTES = 64 * 1024
MAX_PAYLOAD = max(MAX_CONTROL_BYTES, MAX_OBJECTS * (DESCRIPTOR.size + max(BATCH_FRAMES) * 4))


def packet(opcode, sequence, frames=0, count=0, payload=b'', response=False):
    if len(payload) > MAX_PAYLOAD:
        raise ValueError('Object payload exceeds limit')
    for value in (sequence, frames, count):
        if type(value) is not int or not 0 <= value <= 0xffffffff:
            raise ValueError('Invalid object header integer')
    return HEADER.pack(RESPONSE_MAGIC if response else REQUEST_MAGIC, VERSION,
                       opcode, sequence, frames, count, len(payload)) + payload


def parse_header(payload, response=False):
    if len(payload) != HEADER.size:
        raise ValueError('Truncated object header')
    magic, version, opcode, sequence, frames, count, length = HEADER.unpack(payload)
    if magic != (RESPONSE_MAGIC if response else REQUEST_MAGIC) or version != VERSION:
        raise ValueError('Object protocol magic/version differs')
    if length > MAX_PAYLOAD or count > MAX_OBJECTS:
        raise ValueError('Object packet exceeds beta limits')
    return {'opcode': opcode, 'sequence': sequence, 'frames': frames, 'count': count, 'bytes': length}


def read_exact(stream, length, eof_ok=False):
    pieces = bytearray()
    while len(pieces) < length:
        piece = stream.read(length - len(pieces))
        if not piece:
            if not pieces and eof_ok:
                return None
            raise EOFError('Truncated object protocol packet')
        pieces.extend(piece)
    return bytes(pieces)


def json_bytes(value):
    return json.dumps(value, separators=(',', ':'), sort_keys=True, allow_nan=False).encode('utf-8')


def _validate(item, ids, static_types, frames=BLOCK_FRAMES, *, finite_checked=False):
    identifier, kind = item['id'], item.get('type', 1)
    if type(identifier) is not int or not 1 <= identifier <= 0xffffffff or identifier in ids:
        raise ValueError('Invalid or duplicate object ID')
    if type(kind) is not int or not (kind == 1 or (2 <= kind <= 0x20000 and kind & (kind - 1) == 0)):
        raise ValueError('Unsupported original audio object type')
    if kind != 1 and kind in static_types:
        raise ValueError('Duplicate static channel')
    xyz, gain = tuple(item['position']), item.get('gain', 1.0)
    if len(xyz) != 3 or not all(math.isfinite(v) and abs(v) <= 1e6 for v in xyz):
        raise ValueError('Invalid object position')
    if not math.isfinite(gain) or not 0 <= gain <= 1:
        raise ValueError('Invalid source gain')
    samples = item['samples']
    if len(samples) != frames or (not finite_checked and not all(math.isfinite(v) for v in samples)):
        raise ValueError('Expected one finite source mono plane of the negotiated length')
    ids.add(identifier)
    if kind != 1:
        static_types.add(kind)
    return identifier, kind, xyz, gain


def encode_objects(objects, frames=BLOCK_FRAMES):
    if frames not in (BLOCK_FRAMES, *BATCH_FRAMES):
        raise ValueError('Unsupported object quantum size')
    if len(objects) > MAX_OBJECTS:
        raise ValueError('Too many sources for the realtime beta budget')
    output, ids, static_types = bytearray(), set(), set()
    for item in objects:
        identifier, kind, xyz, gain = _validate(item, ids, static_types, frames)
        audio = array.array('f', item['samples'])
        if not all(math.isfinite(value) for value in audio):
            raise ValueError('Object samples must be finite float32 values')
        if sys.byteorder != 'little':
            audio.byteswap()
        output.extend(DESCRIPTOR.pack(identifier, kind, *xyz, gain))
        output.extend(audio.tobytes())
    return bytes(output)


def _decode_sources(payload, frames, count, allowed_frames):
    record_bytes = DESCRIPTOR.size + frames * 4
    if (frames not in allowed_frames or type(count) is not int or not 0 <= count <= MAX_OBJECTS
            or len(payload) != record_bytes * count):
        raise ValueError('Object count/frames/payload differs from the beta format')
    output, ids, static_types = [], set(), set()
    for index in range(count):
        offset = index * record_bytes
        identifier, kind, x, y, z, gain = DESCRIPTOR.unpack_from(payload, offset)
        # Inspect only the mono plane, never the ID/type/XYZ/gain descriptor.
        mono = payload[offset + DESCRIPTOR.size:offset + record_bytes]
        if not is_finite_f32le(mono):
            raise ValueError('Expected one finite source mono plane of the negotiated length')
        samples = array.array('f')
        samples.frombytes(mono)
        if sys.byteorder != 'little':
            samples.byteswap()
        item = {'id': identifier, 'type': kind, 'position': (x, y, z), 'gain': gain, 'samples': samples}
        _validate(item, ids, static_types, frames, finite_checked=True)
        output.append(item)
    return output


def decode_objects(payload, frames, count):
    return _decode_sources(payload, frames, count, (BLOCK_FRAMES,))


def decode_batch(payload, frames, count):
    """XYZ/gain are held across this 2/4-block transport quantum."""
    return _decode_sources(payload, frames, count, BATCH_FRAMES)
