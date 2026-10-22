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
from protocol import READY, PROCESS, CONFIGURE, QUIT, ERROR, HEADER, encode, decode_header, json_bytes, read_exact


POST_PINS = {
    'adapter': 'a8ef19e62e00bf7fd33357c302cac9ca124fbe05011d79ecf8904ebcf608d2b4',
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
                             scope='Official profile controls through original Hrtf frontend, followed by a separate original VLLDP160 speaker stage candidate. PCM channel beds only. Live Windows VLLDP version/order and full endpoint equality are unverified.')

    def process(self, payload):
        if self.closed:
            raise RuntimeError('Original speaker stream is closed')
        return self.post.process(self.frontend.process(payload))

    def apply_controls(self, fields):
        return self.frontend.apply_controls(fields)

    def process_objects(self, objects):
        if self.closed:
            raise RuntimeError('Original speaker stream is closed')
        return self.post.process(self.frontend.process_objects(objects))

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


def _speaker_post_paths(ns, selected_post=None):
    names = ('speaker_post_adapter', 'speaker_post_dll', 'speaker_post_plan', 'speaker_post_shim')
    values = [getattr(ns, name, None) for name in names]
    if ns.engine != 'hrtf-speakers':
        if any(values):
            raise ValueError('The speaker post-stage is forbidden for other output profiles')
        return None
    if not all(values):
        raise ValueError('Speaker Game requires all verified original post-stage paths')
    paths = {key: Path(value) for key, value in zip(POST_PINS, values)}
    if selected_post is not None:
        paths['plan'] = selected_post[0]
    for key, path in paths.items():
        expected = selected_post[1] if key == 'plan' and selected_post is not None else POST_PINS[key]
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Speaker post-stage asset pin differs: ' + key)
    paths = {key: path.resolve() for key, path in paths.items()}
    if paths['shim'].name.lower() != 'audioeng.dll' or paths['shim'].parent != paths['dll'].parent:
        raise ValueError('The verified allocator shim must be adjacent to the speaker DLL')
    return paths


def _profile_configuration(ns):
    catalog_name = getattr(ns, 'profile_catalog', None)
    if not catalog_name:
        return None
    if ns.engine not in ('hrtf-speakers', 'hrtf-headphones'):
        raise ValueError('Profile catalogs require the verified full frontend')
    catalog_path = Path(catalog_name)
    if catalog_path.is_symlink() or not catalog_path.is_file() or catalog_path.stat().st_size > 1024 * 1024:
        raise ValueError('Expected a bounded regular profile catalog')
    raw = catalog_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != getattr(ns, 'profile_catalog_sha256', None):
        raise ValueError('Private profile catalog changed')
    catalog = json.loads(raw)
    if catalog.get('schema') != 'ally-dsp-original-profiles-v1':
        raise ValueError('Unknown private profile catalog')
    if ns.preset not in ('game', 'dynamic', 'movie', 'music', 'voice') or ns.voicing not in ('balanced', 'detailed', 'warm'):
        raise ValueError('Unknown original preset or voicing')
    route = 'speakers' if ns.engine == 'hrtf-speakers' else 'headphones'
    profile = catalog['routes'][route]['profiles'][ns.preset]
    fields = dict(profile['cp'])
    voicing = profile.get('voicings', {}).get(ns.voicing)
    if voicing:
        fields['ieq-bands-set'] = voicing
    selected_post = None
    post = profile.get('post_plan')
    if post is not None:
        relative = Path(post['path'])
        if relative.is_absolute() or not relative.parts or any(part in ('.', '..') for part in relative.parts):
            raise ValueError('Profile post-stage path must stay in the private generation')
        parent = catalog_path.resolve().parent
        path = parent.joinpath(relative)
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(parent):
            raise ValueError('Profile post-stage path left the private generation')
        selected_post = (path, post['sha256'])
    if (route == 'speakers') != (selected_post is not None):
        raise ValueError('Only speaker profiles require an original speaker post-stage')
    return {'route': route, 'fields': fields, 'post': selected_post, 'namespace': ns,
            'preset': ns.preset, 'voicing': ns.voicing, 'catalog_sha256': hashlib.sha256(raw).hexdigest()}


def create_engine(ns, *, object_mapper=False):
    # Validate the profile and every additional asset before initializing a DSP.
    profile_config = _profile_configuration(ns)
    post_paths = _speaker_post_paths(ns, profile_config['post'] if profile_config else None)
    if ns.engine == 'vr-game':
        if object_mapper:
            raise ValueError('Object rendering requires the full original frontend')
        if not ns.plan:
            raise ValueError('vr-game requires --plan')
        from vr_adapter import VrGameAdapter
        return VrGameAdapter(dll=ns.dll, plan=ns.plan, property_store=ns.property_store,
                             input_channels=ns.input_channels)
    if not all((ns.frontend_adapter, ns.hrtf, ns.init_blob, ns.initial_runtime_blob, ns.runtime_blob)):
        raise ValueError('Full frontend requires explicit adapter/DLL/blob paths')
    from hrtf_adapter import HrtfStreamAdapter
    mapper_options = {'object_mapper': True} if object_mapper else {}
    frontend = HrtfStreamAdapter(profile=ns.engine, adapter=ns.frontend_adapter, dll=ns.dll,
                                 hrtf=ns.hrtf, init_blob=ns.init_blob,
                                 initial_runtime_blob=ns.initial_runtime_blob, runtime_blob=ns.runtime_blob,
                                 property_store=ns.property_store, input_channels=ns.input_channels, **mapper_options)
    if profile_config is not None:
        try:
            frontend.apply_controls(profile_config['fields'])
        except BaseException:
            frontend.close()
            raise
        frontend.metadata.update(runtime_controls=True, original_preset=ns.preset,
                                 original_voicing=ns.voicing, profile_catalog_sha256=profile_config['catalog_sha256'])
        frontend.control_profile = profile_config
    if post_paths is None:
        return frontend
    post = None
    try:
        shim = ctypes.WinDLL(str(post_paths['shim']))
        spec = importlib.util.spec_from_file_location('_original_speaker_post', post_paths['adapter'])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        post = module.VlldpStreamAdapter(dll=post_paths['dll'], plan=post_paths['plan'],
                                        plan_sha256=profile_config['post'][1] if profile_config else POST_PINS['plan'], property_store=ns.property_store,
                                        variant='game')
        engine = SpeakerPostStream(frontend, post, shim)
        if profile_config is not None:
            engine.control_profile = profile_config
        return engine
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


def argument_parser():
    """Identical pinned asset/profile arguments for PCM and object workers."""
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
    ap.add_argument('--profile-catalog')
    ap.add_argument('--profile-catalog-sha256')
    ap.add_argument('--preset', default='game')
    ap.add_argument('--voicing', default='balanced')
    ap.add_argument('--property-store', required=True)
    ap.add_argument('--input-channels', type=int, required=True)
    return ap


def configure_engine(engine, request):
    """Apply full official profile plus user overrides to an existing DSP."""
    if not hasattr(engine, 'control_profile'):
        raise ValueError('Runtime controls require a current verified profile catalog')
    if not isinstance(request, dict) or request.get('schema') != 'ally-dsp-original-controls-v1':
        raise ValueError('Unknown runtime control request')
    from runtime_controls import normalize_controls, controls_to_fields, fields_to_controls
    controls = normalize_controls(request.get('controls'))
    profile = engine.control_profile
    preset, voicing = request.get('preset', profile['preset']), request.get('voicing', profile['voicing'])
    if preset != profile['preset'] or voicing != profile['voicing']:
        namespace = argparse.Namespace(**dict(vars(profile['namespace']), preset=preset, voicing=voicing))
        updated = _profile_configuration(namespace)
        old_hash = profile['post'][1] if profile['post'] else None
        new_hash = updated['post'][1] if updated['post'] else None
        if old_hash != new_hash:
            raise ValueError('This profile requires restarting its speaker post-stage')
        profile = updated
    fields = controls_to_fields(controls, profile['fields'], route=profile['route'])
    audit = engine.apply_controls(fields)
    engine.control_profile = profile
    return {'applied': True, 'controls': controls, 'effective': fields_to_controls(fields),
            'preset': profile['preset'], 'voicing': profile['voicing'], 'audit': audit}


def main():
    ns = argument_parser().parse_args()
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
            if header['opcode'] == CONFIGURE:
                if header['frames'] or header['channels'] or not hasattr(engine, 'control_profile'):
                    raise ValueError('Runtime controls require a current verified profile catalog')
                request = json.loads(read_exact(source, header['payload_bytes']))
                send(CONFIGURE, sequence, payload=json_bytes(configure_engine(engine, request)))
                continue
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
