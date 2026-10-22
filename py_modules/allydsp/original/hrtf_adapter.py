"""Stream interface for the separately frozen, verified original Hrtf frontend."""
import hashlib
import importlib.util
from pathlib import Path
import sys
import time

try:
    from .protocol import is_finite_f32le
except ImportError:
    from protocol import is_finite_f32le

PINS = {
    'adapter': '57a8c5203050a666cbb0cfdf8079acff56aa48fab0277beaf5fc1adbc0294c78',
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
                 runtime_blob, property_store, input_channels, object_mapper=False):
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
        mapper_options = {'object_mapper': True} if object_mapper else {}
        self.delegate = module.PcmFrontendAdapter(
            paths['dll'], paths['hrtf'], paths['init_blob'], paths['initial_runtime_blob'],
            input_channels=input_channels, mode=0 if profile == 'hrtf-headphones' else 1,
            runtime_blob=paths['runtime_blob'], **mapper_options)
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
        self.control_updates = 0
        self.last_control_audit = None
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
                             speaker_level_state_phase='initial-verified-game-before-profile-or-user-controls',
                             persistent_dsp_instance=True, init_seconds=(time.perf_counter_ns() - started) / 1e9,
                             scope='Original HrtfEnc SetAudioObject + metadata generation + EncodeAudioData. PCM channel beds only; unavailable game Atmos object metadata is not recreated.')
        if object_mapper:
            self.metadata.update(object_mapper=True, static_audio_object_layout=None,
                                 scope='Original mapper and HrtfEnc process explicit static/dynamic source objects. No metadata is inferred from mixed PCM; Windows endpoint equivalence is unverified.')

    def controls_readback(self):
        """Read the pinned receiver's state; never write original DSP memory."""
        c, dsp = self._module.C, self.delegate.engine.dsp
        integer_offsets = {
            'eqEnabled': 0xb84, 'ieqEnabled': 0x7dc,
            'dialogEnabled': 0x794, 'dialogAmount': 0x79c,
            'volumeLevelerEnabled': 0x7c4,
            'volumeLevelerAmount': 0x7bc, 'volumeLevelerDrcEnabled': 0x7d4,
        }
        result = {key: c.c_int32.from_address(dsp + offset).value
                  for key, offset in integer_offsets.items()}
        result.update(
            eqBandsRaw=list((c.c_int32 * 20).from_address(dsp + 0xb88)),
            eqFrequencyCount=c.c_uint32.from_address(dsp + 0xd20).value,
            eqFrequencies=list((c.c_int32 * 20).from_address(dsp + 0xd24)),
            ieqAmountInternal=c.c_float.from_address(dsp + 0x7e4).value,
            configuration=self.delegate.engine.state_readback(),
        )
        return result

    def apply_controls(self, fields):
        """Deliver a complete selected profile plus overrides via normal CAPX.

        A reset supplies that profile's full fields again. Sending an empty
        delta would leave previous values in the persistent original DSP.
        Applied mode changes become visible after the next processing block.
        """
        if self.closed:
            raise RuntimeError('Original frontend is closed')
        try:
            from .runtime_controls import encode_runtime
        except ImportError:
            from runtime_controls import encode_runtime
        blob = encode_runtime(fields)
        digest = hashlib.sha256(blob).hexdigest()
        notification = self.delegate.engine.notify(blob)
        _verify_runtime_notification(notification, digest)
        engine = self.delegate.engine
        # The frozen offline adapter records property reads for research. Keep
        # that diagnostic history bounded during repeated live slider updates.
        for history in (getattr(getattr(engine, 'user', None), 'audit', None), getattr(engine, 'audit', None)):
            if isinstance(history, list) and len(history) > 64:
                del history[:-64]
        if not engine.engine_guard.check() or not engine.plugin_guard.check():
            raise RuntimeError('Original frontend guard changed during control delivery')
        self.control_updates += 1
        audit = {
            'schema': 'original-dll-control-delivery-v1',
            'update': self.control_updates, 'blob_sha256': digest,
            'blob_bytes': len(blob), 'notification_verified': True,
            'same_module': True, 'same_dsp': True,
            'readback': self.controls_readback(),
            'readback_phase': 'after-notification-before-next-process',
        }
        self.last_control_audit = audit
        self.metadata['last_control_audit'] = audit
        return audit

    def process(self, payload):
        if self.closed or len(payload) != self.block_frames * self.input_channels * 4:
            raise ValueError('Expected one fixed PCM block in an open frontend')
        if not is_finite_f32le(payload):
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
        if not is_finite_f32le(output):
            raise RuntimeError('Nonfinite original full frontend output')
        return output

    def process_objects(self, objects):
        """Render an already validated object batch through the same pinned DSP."""
        if self.closed or not self.metadata.get('object_mapper'):
            raise RuntimeError('Expected an open original object frontend')
        started = time.perf_counter_ns()
        output = self.delegate.engine.process_objects(objects)
        elapsed = time.perf_counter_ns() - started
        self.processing_ns += elapsed
        self.max_processing_ns = max(self.max_processing_ns, elapsed)
        self.frontend_processing_seconds += self.delegate.engine.timings.pop()
        self.delegate.blocks += 1
        if len(output) != 2048 or not is_finite_f32le(output):
            raise RuntimeError('Invalid original object frontend output')
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
                    control_updates=self.control_updates,
                    last_control_audit=self.last_control_audit,
                    speaker_level_state_sha256=level_fingerprint,
                    speaker_level_state_reference='initial-speaker-game-only; other profiles and user controls may differ',
                    speaker_level_state_verified=(level_fingerprint == PINS['speaker_level_state']
                                                  if level_fingerprint is not None else False))
