"""Pinned original-DLL explicit-object worker over bounded binary stdio."""
import os
from pathlib import Path
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from object_protocol import (HEADER, READY, PROCESS, RESET, QUIT, CONFIGURE, BATCH, ERROR,
                             BLOCK_FRAMES, packet, parse_header, read_exact, json_bytes)
from object_engine import ObjectBatchEngine
from windows_worker import argument_parser
import json


def serve(namespace, source, sink, *, factory=ObjectBatchEngine):
    """One dedicated object stream; external relay owns sequence translation."""
    engine, sequence = None, 0
    def send(opcode, seq, frames=0, count=0, payload=b''):
        sink.write(packet(opcode, seq, frames, count, payload, response=True))
        sink.flush()
    try:
        engine = factory(namespace)
        send(READY, 0, payload=json_bytes(dict(engine.metadata,
             schema='original-dll-object-worker-v1', python_version=sys.version, pointer_bits=64)))
        started = time.perf_counter_ns()
        while True:
            raw = read_exact(source, HEADER.size, eof_ok=True)
            if raw is None:
                raise EOFError('Explicit QUIT or RESET is required for complete object stream audit')
            header = parse_header(raw)
            sequence += 1
            if header['sequence'] != sequence:
                raise ValueError('Expected monotonically consecutive object request sequence')
            opcode = header['opcode']
            if opcode in (QUIT, RESET):
                if header['bytes'] or header['frames'] or header['count']:
                    raise ValueError('QUIT/RESET must have no source payload')
                summary = engine.reset() if opcode == RESET else engine.close()
                summary.update(clean_quit=True, stream_seconds=(time.perf_counter_ns() - started) / 1e9)
                send(opcode, sequence, payload=json_bytes(summary))
                return 0
            if opcode == CONFIGURE:
                if header['frames'] or header['count']:
                    raise ValueError('CONFIGURE must have no audio format')
                request = json.loads(read_exact(source, header['bytes']))
                send(CONFIGURE, sequence, payload=json_bytes(engine.configure(request)))
                continue
            if opcode == BATCH:
                output = engine.process_batch(read_exact(source, header['bytes']), header['frames'], header['count'])
                send(BATCH, sequence, frames=header['frames'], count=2, payload=output)
                continue
            if opcode != PROCESS or header['frames'] != BLOCK_FRAMES:
                raise ValueError('Unknown object request or fixed block size differs')
            output = engine.process(read_exact(source, header['bytes']), header['frames'], header['count'])
            send(PROCESS, sequence, frames=BLOCK_FRAMES, count=2, payload=output)
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        try:
            send(ERROR, sequence, payload=json_bytes({'error': type(error).__name__, 'message': str(error)}))
        except Exception:
            pass
        return 1
    finally:
        if engine is not None and not engine.closed:
            engine.close()


def main():
    parser = argument_parser()
    parser.description = 'Original Dolby source-object beta: at most 32 sources including the static bed'
    parser.add_argument('--object-bypass', action='store_true')
    namespace = parser.parse_args()
    if sys.platform != 'win32':
        raise RuntimeError('Use 64-bit Windows Python in a dedicated Proton prefix for this worker')
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    return serve(namespace, sys.stdin.buffer, sys.stdout.buffer)


if __name__ == '__main__':
    raise SystemExit(main())
