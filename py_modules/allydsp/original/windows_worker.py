"""Persistent original-DLL PCM worker. Binary stdout, diagnostics on stderr."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from protocol import READY, PROCESS, QUIT, ERROR, HEADER, encode, decode_header, json_bytes, read_exact


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--engine', choices=['vr-game', 'hrtf-headphones', 'hrtf-speakers'], default='vr-game')
    ap.add_argument('--dll', required=True)
    ap.add_argument('--plan')
    ap.add_argument('--frontend-adapter')
    ap.add_argument('--hrtf')
    ap.add_argument('--init-blob')
    ap.add_argument('--initial-runtime-blob')
    ap.add_argument('--runtime-blob')
    ap.add_argument('--property-store', required=True)
    ap.add_argument('--input-channels', type=int, required=True)
    ns = ap.parse_args()
    if sys.platform != 'win32':
        raise RuntimeError('Use 64-bit Windows Python in Proton for this worker')
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    source, sink = sys.stdin.buffer, sys.stdout.buffer
    engine = None
    sequence = 0
    try:
        if ns.engine == 'vr-game':
            if not ns.plan:
                raise ValueError('vr-game requires --plan')
            from vr_adapter import VrGameAdapter
            engine = VrGameAdapter(dll=ns.dll, plan=ns.plan, property_store=ns.property_store,
                                   input_channels=ns.input_channels)
        else:
            if not all((ns.frontend_adapter, ns.hrtf, ns.init_blob, ns.initial_runtime_blob, ns.runtime_blob)):
                raise ValueError('Full frontend requires explicit adapter/DLL/blob paths')
            from hrtf_adapter import HrtfStreamAdapter
            engine = HrtfStreamAdapter(profile=ns.engine, adapter=ns.frontend_adapter, dll=ns.dll,
                                       hrtf=ns.hrtf, init_blob=ns.init_blob,
                                       initial_runtime_blob=ns.initial_runtime_blob, runtime_blob=ns.runtime_blob,
                                       property_store=ns.property_store, input_channels=ns.input_channels)
        def send(opcode, seq, frames=0, channels=0, payload=b''):
            sink.write(encode(opcode, seq, frames, channels, payload, response=True))
            sink.flush()
        metadata = dict(engine.metadata, schema='original-dll-pcm-worker-v1', protocol_version=1,
                        python_version=sys.version, pointer_bits=64)
        send(READY, 0, payload=json_bytes(metadata))
        started = time.perf_counter_ns()
        while True:
            raw = read_exact(source, HEADER.size, eof_ok=True)
            if raw is None:
                raise EOFError('Explicit QUIT is required for complete stream audit')
            header = decode_header(raw)
            sequence += 1
            if header['sequence'] != sequence:
                raise ValueError('Expected monotonically consecutive request sequence')
            if header['opcode'] == QUIT:
                if header['payload_bytes'] or header['frames'] or header['channels']:
                    raise ValueError('QUIT must have no audio payload')
                summary = engine.close()
                summary.update(schema='original-dll-pcm-worker-summary-v1',
                               stream_seconds=(time.perf_counter_ns() - started) / 1e9,
                               clean_quit=True)
                send(QUIT, sequence, payload=json_bytes(summary))
                return 0
            if (header['opcode'] != PROCESS or header['frames'] != engine.block_frames
                    or header['channels'] != engine.input_channels
                    or header['payload_bytes'] != engine.block_frames * engine.input_channels * 4):
                raise ValueError('PROCESS header differs from negotiated fixed PCM format')
            payload = read_exact(source, header['payload_bytes'])
            output = engine.process(payload)
            send(PROCESS, sequence, engine.block_frames, engine.output_channels, output)
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        try:
            sink.write(encode(ERROR, sequence, payload=json_bytes({'error': type(error).__name__,
                                                                  'message': str(error)}), response=True))
            sink.flush()
        except Exception:
            pass
        return 1
    finally:
        if engine is not None and not engine.closed:
            engine.close()


if __name__ == '__main__':
    raise SystemExit(main())
