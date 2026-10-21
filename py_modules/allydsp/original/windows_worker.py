"""Persistent original-DLL PCM worker. Binary stdout, diagnostics on stderr."""
import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from protocol import READY, PROCESS, QUIT, ERROR, HEADER, encode, decode_header, json_bytes, read_exact


POST_PINS = {
    'adapter': 'e2c6c7c2a8acdcaa87f00598a52635ff786d52944f1858f7e4514ba3b600ee6e',
    'dll': '28990e8d5845b012020b52947ceab0cf37199ede1e8162cd7d7a03db580af82c',
    'plan': 'f36a04973a6e373b8ba0cb734c14ac6d2d0d401ffe4347be448091940e3348af',
    'shim': '00d8b24227cb93d7576e3b07da1cdc25b2377e946b7eaa586c98b8d5e0b87a43',
}


class SpeakerPostStream:
    """Sequential speaker-only original frontend and original VLLDP candidate."""
    def __init__(self, frontend, post, shim):
        if (post.block_frames, post.sample_rate, post.input_channels, post.output_channels) != (
                frontend.block_frames, frontend.sample_rate, frontend.output_channels, frontend.output_channels):
            raise ValueError('Original speaker stages negotiated different PCM formats')
        self.frontend, self.post, self._shim = frontend, post, shim
        self.block_frames, self.sample_rate = frontend.block_frames, frontend.sample_rate
        self.input_channels, self.output_channels = frontend.input_channels, frontend.output_channels
        self.closed = False
        self.metadata = dict(frontend.metadata, engine=frontend.metadata['engine'] + '-vlldp160-post',
                             speaker_post=post.metadata,
                             additional_post_latency_frames=post.metadata['declared_latency_frames'],
                             scope='Official Game controls through original Hrtf frontend, followed by a separate original VLLDP160 speaker stage candidate. PCM channel beds only. Live Windows VLLDP version/order and full endpoint equality are unverified.')

    def process(self, payload):
        if self.closed:
            raise RuntimeError('Original speaker stream is closed')
        return self.post.process(self.frontend.process(payload))

    def close(self):
        if self.closed:
            raise RuntimeError('Original speaker stream is already closed')
        try:
            post_summary = self.post.close()
        finally:
            try:
                frontend_summary = self.frontend.close()
            finally:
                self.closed = True
        return dict(frontend_summary, speaker_post=post_summary,
                    guards_unchanged=frontend_summary['guards_unchanged'] and post_summary['guards_unchanged'],
                    same_dsp_instance=frontend_summary['same_dsp_instance'] and post_summary['same_dsp_instance'])


def _speaker_post_paths(ns):
    names = ('speaker_post_adapter', 'speaker_post_dll', 'speaker_post_plan', 'speaker_post_shim')
    values = [getattr(ns, name, None) for name in names]
    if ns.engine != 'hrtf-speakers':
        if any(values):
            raise ValueError('The speaker post-stage is forbidden for other output profiles')
        return None
    if not all(values):
        raise ValueError('Speaker Game requires all verified original post-stage paths')
    paths = {key: Path(value) for key, value in zip(POST_PINS, values)}
    for key, path in paths.items():
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != POST_PINS[key]:
            raise ValueError('Speaker post-stage asset pin differs: ' + key)
    paths = {key: path.resolve() for key, path in paths.items()}
    if paths['shim'].name.lower() != 'audioeng.dll' or paths['shim'].parent != paths['dll'].parent:
        raise ValueError('The verified allocator shim must be adjacent to the speaker DLL')
    return paths


def create_engine(ns):
    # Validate the profile and every additional asset before initializing a DSP.
    post_paths = _speaker_post_paths(ns)
    if ns.engine == 'vr-game':
        if not ns.plan:
            raise ValueError('vr-game requires --plan')
        from vr_adapter import VrGameAdapter
        return VrGameAdapter(dll=ns.dll, plan=ns.plan, property_store=ns.property_store,
                             input_channels=ns.input_channels)
    if not all((ns.frontend_adapter, ns.hrtf, ns.init_blob, ns.initial_runtime_blob, ns.runtime_blob)):
        raise ValueError('Full frontend requires explicit adapter/DLL/blob paths')
    from hrtf_adapter import HrtfStreamAdapter
    frontend = HrtfStreamAdapter(profile=ns.engine, adapter=ns.frontend_adapter, dll=ns.dll,
                                 hrtf=ns.hrtf, init_blob=ns.init_blob,
                                 initial_runtime_blob=ns.initial_runtime_blob, runtime_blob=ns.runtime_blob,
                                 property_store=ns.property_store, input_channels=ns.input_channels)
    if post_paths is None:
        return frontend
    post = None
    try:
        shim = ctypes.WinDLL(str(post_paths['shim']))
        spec = importlib.util.spec_from_file_location('_original_speaker_post', post_paths['adapter'])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        post = module.VlldpStreamAdapter(dll=post_paths['dll'], plan=post_paths['plan'],
                                        plan_sha256=POST_PINS['plan'], property_store=ns.property_store,
                                        variant='game')
        return SpeakerPostStream(frontend, post, shim)
    except BaseException:
        # Preserve the startup failure. The dedicated worker then exits; do
        # not leave a successfully constructed stage without its close audit.
        for stage in (post, frontend):
            if stage is not None:
                try:
                    stage.close()
                except Exception:
                    traceback.print_exc(file=sys.stderr)
        raise


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
    ap.add_argument('--speaker-post-adapter')
    ap.add_argument('--speaker-post-dll')
    ap.add_argument('--speaker-post-plan')
    ap.add_argument('--speaker-post-shim')
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
        engine = create_engine(ns)
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
