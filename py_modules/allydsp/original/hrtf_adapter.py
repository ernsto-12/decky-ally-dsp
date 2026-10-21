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
    'hrtf-speakers': '0227a54537b0405ca85a8d321dcbb2ffe3fa94be28ddfbfa7c3bc0d46bf97d71',
}


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
        return dict(summary, processed_blocks=self.delegate.blocks,
                    guards_unchanged=summary['all_host_guards_unchanged'], same_dsp_instance=same,
                    original_processing_seconds=self.frontend_processing_seconds,
                    dsp_seconds=self.processing_ns / 1e9,
                    max_dsp_block_seconds=self.max_processing_ns / 1e9)
