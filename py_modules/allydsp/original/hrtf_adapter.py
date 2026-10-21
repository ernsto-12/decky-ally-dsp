"""Stream interface for the separately frozen, verified original Hrtf frontend."""
import array
import hashlib
import importlib.util
import math
from pathlib import Path
import sys
import time

PINS = {
    'adapter': '265d63f39de0b43fdef6096803107599d527b58a63596e861ab63f2ef749e0f3',
    'property_store': 'f5cfc1d90ce8c58ab70bf700a941645bed340ed4f54b31a41d4800473877c0b4',
    'dll': '0fada40251880d03c657a357745d362799c93fb6024f733f1c8bf1057f5e4715',
    'hrtf': '3da34330b36a79d6c4e42220190ee8239cad65564911c248ebda59d237bf9f88',
    'init_blob': 'b6be9fd91f09bab641f99db05d02b8f19fd93f64e5c0e6c425048c0357c5b168',
    'initial_runtime_blob': '59250bb431a88665fe095477fee0fbea30c928cc038ff34fd83313bd8d0bedd8',
    'hrtf-headphones': 'cdda85711544f2640c674dbae82878f5c83f1441c365f94960f3875220428289',
    'hrtf-speakers': '88e29379fe4007d623daee006eb9b7e6786db7fb79e49401dfef989b648cfb1a',
    # Fingerprint of the verified Game level-control state, not OEM values.
    'speaker_level_state': '00269187b1fd7947149763497d1b0b830cea3758a68f7a508326eeb4f453eaca',
}


def _speaker_level_fingerprint(module, engine):
    # Read-only state locations verified for the pinned processing DLL. The
    # ordered four integer and eight float slots contain no pointers or clocks.
    offsets = (0x7c4, 0x7d4, 0x7bc, 0x7b4, 0x71c, 0x734,
               0x72c, 0x6fc, 0x704, 0x70c, 0x714, 0x724)
    return hashlib.sha256(b''.join(module.C.string_at(engine.dsp + offset, 4)
                                   for offset in offsets)).hexdigest()


def _verify_runtime_notification(notification, expected_hash):
    """Fail before audio starts if the property callback reached another engine.

    The exact runtime pack is private. Its digest must be observed both at the
    notification boundary and by the original receiver's property-store read.
    This verifies delivery, not equivalence with a Windows hardware endpoint.
    """
    if (not isinstance(notification, dict)
            or notification.get('blob_sha256') != expected_hash
            or notification.get('same_module') is not True
            or notification.get('same_dsp') is not True):
        raise RuntimeError('Original runtime notification did not retain the verified engine')
    queries = notification.get('callback_queries')
    if (not isinstance(queries, list)
            or not any(isinstance(query, dict) and query.get('supplied') is True
                       and query.get('kind') == 'blob_bytes'
                       and query.get('sha256') == expected_hash for query in queries)):
        raise RuntimeError('Original receiver did not read the verified runtime property')


class HrtfStreamAdapter:
    def __init__(self, *, profile, adapter, dll, hrtf, init_blob, initial_runtime_blob,
                 runtime_blob, property_store, input_channels):
        started = time.perf_counter_ns()
        if profile not in ('hrtf-headphones', 'hrtf-speakers') or input_channels not in (1, 2, 6, 8):
            raise ValueError('Unsupported original frontend profile or channel count')
        paths = {key: Path(value).resolve() for key, value in {
            'adapter': adapter, 'dll': dll, 'hrtf': hrtf, 'init_blob': init_blob,
            'initial_runtime_blob': initial_runtime_blob, 'runtime_blob': runtime_blob,
            'property_store': property_store}.items()}
        for key, path in paths.items():
            pin = PINS[profile if key == 'runtime_blob' else key]
            if hashlib.sha256(path.read_bytes()).hexdigest() != pin:
                raise ValueError('Original frontend asset pin differs: ' + key)
        if paths['property_store'] != paths['adapter'].with_name('local_property_store.py'):
            raise ValueError('Frozen frontend must import the explicitly pinned adjacent property store')
        sys.path.insert(0, str(paths['adapter'].parent))
        spec = importlib.util.spec_from_file_location('_original_full_frontend', paths['adapter'])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._module = module
        self.delegate = module.PcmFrontendAdapter(
            paths['dll'], paths['hrtf'], paths['init_blob'], paths['initial_runtime_blob'],
            input_channels=input_channels, mode=0 if profile == 'hrtf-headphones' else 1,
            runtime_blob=paths['runtime_blob'])
        _verify_runtime_notification(self.delegate.metadata.get('runtime_notification'), PINS[profile])
        level_fingerprint = None
        if profile == 'hrtf-speakers':
            level_fingerprint = _speaker_level_fingerprint(module, self.delegate.engine)
            if level_fingerprint != PINS['speaker_level_state']:
                raise RuntimeError('Original speaker Game level-control readback differs')
        self.block_frames, self.sample_rate = 256, 48000
        self.input_channels, self.output_channels = input_channels, 2
        self.closed = False
        self.processing_ns = 0
        self.max_processing_ns = 0
        self.frontend_processing_seconds = 0.0
        self.metadata = dict(self.delegate.metadata,
                             engine='original-full-frontend-' + profile.removeprefix('hrtf-'),
                             profile=profile, adapter_sha256=PINS['adapter'], dll_sha256=PINS['dll'],
                             hrtf_sha256=PINS['hrtf'], runtime_blob_sha256=PINS[profile],
                             init_blob_sha256=PINS['init_blob'], initial_runtime_blob_sha256=PINS['initial_runtime_blob'],
                             runtime_notification_verified=True,
                             configuration_readback=self.delegate.engine.state_readback(),
                             configuration_readback_phase='after-notification-before-first-process',
                             speaker_level_state_sha256=level_fingerprint,
                             speaker_level_state_verified=profile == 'hrtf-speakers',
                             persistent_dsp_instance=True, init_seconds=(time.perf_counter_ns() - started) / 1e9,
                             scope='Original HrtfEnc SetAudioObject + metadata generation + EncodeAudioData. PCM channel beds only; unavailable game Atmos object metadata is not recreated.')

    def process(self, payload):
        if self.closed or len(payload) != self.block_frames * self.input_channels * 4:
            raise ValueError('Expected one fixed PCM block in an open frontend')
        values = array.array('f')
        values.frombytes(payload)
        if not all(math.isfinite(v) for v in values):
            raise ValueError('Nonfinite PCM input')
        started = time.perf_counter_ns()
        output = self.delegate.process(payload)
        elapsed = time.perf_counter_ns() - started
        self.processing_ns += elapsed
        self.max_processing_ns = max(self.max_processing_ns, elapsed)
        # The frozen research adapter keeps per-block timings for offline
        # analysis. Consume each sample here to keep long game sessions bounded.
        self.frontend_processing_seconds += self.delegate.engine.timings.pop()
        if len(output) != 2048:
            raise RuntimeError('Original full frontend output size differs')
        values = array.array('f')
        values.frombytes(output)
        if not all(math.isfinite(v) for v in values):
            raise RuntimeError('Nonfinite original full frontend output')
        return output

    def close(self):
        self.closed = True
        summary = self.delegate.close()
        engine = self.delegate.engine
        same = (self._module.getptr(engine.processing + 0x538) == engine.module
                and self._module.getptr(engine.module + 0x60) == engine.dsp)
        level_fingerprint = (_speaker_level_fingerprint(self._module, engine)
                             if self.metadata['profile'] == 'hrtf-speakers' else None)
        return dict(summary, processed_blocks=self.delegate.blocks,
                    guards_unchanged=summary['all_host_guards_unchanged'], same_dsp_instance=same,
                    original_processing_seconds=self.frontend_processing_seconds,
                    dsp_seconds=self.processing_ns / 1e9,
                    max_dsp_block_seconds=self.max_processing_ns / 1e9,
                    configuration_readback=engine.state_readback(),
                    configuration_readback_phase=('after-last-process' if self.delegate.blocks else 'before-first-process'),
                    speaker_level_state_sha256=level_fingerprint,
                    speaker_level_state_verified=(level_fingerprint == PINS['speaker_level_state']
                                                  if level_fingerprint is not None else False))
