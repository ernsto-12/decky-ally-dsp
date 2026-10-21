"""Version 1 PCM protocol shared by the Linux client and Windows worker.

The audio transport carries little-endian IEEE float32 interleaved PCM. It never
opens an audio device. stdout belongs exclusively to this protocol.
"""
import json
import struct

HEADER = struct.Struct('<4sHHIIII')
REQUEST_MAGIC = b'ADSP'
RESPONSE_MAGIC = b'ADSR'
VERSION = 1
READY, PROCESS, QUIT, ERROR = 0, 1, 3, 0xFFFF
MAX_PAYLOAD = 1024 * 1024


def encode(opcode, sequence, frames=0, channels=0, payload=b'', *, response=False):
    if len(payload) > MAX_PAYLOAD:
        raise ValueError('Payload exceeds protocol limit')
    return HEADER.pack(RESPONSE_MAGIC if response else REQUEST_MAGIC, VERSION,
                       opcode, sequence, frames, channels, len(payload)) + payload


def decode_header(raw, *, response=False):
    if len(raw) != HEADER.size:
        raise ValueError('Truncated protocol header')
    magic, version, opcode, sequence, frames, channels, size = HEADER.unpack(raw)
    if magic != (RESPONSE_MAGIC if response else REQUEST_MAGIC) or version != VERSION:
        raise ValueError('Protocol magic/version differs')
    if size > MAX_PAYLOAD:
        raise ValueError('Payload exceeds protocol limit')
    return {'opcode': opcode, 'sequence': sequence, 'frames': frames,
            'channels': channels, 'payload_bytes': size}


def json_bytes(value):
    return json.dumps(value, allow_nan=False, separators=(',', ':')).encode('utf-8')


def read_exact(stream, size, *, eof_ok=False):
    output = bytearray()
    while len(output) < size:
        piece = stream.read(size - len(output))
        if not piece:
            if not output and eof_ok:
                return None
            raise EOFError('Truncated PCM protocol packet')
        output.extend(piece)
    return bytes(output)
