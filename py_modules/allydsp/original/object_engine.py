"""Process-scoped explicit-object engine using the product's pinned assets.

No game interception, TCP listener or PipeWire routing is performed here. The
caller owns the dedicated process lifetime, including replacement on RESET.
"""
import array
import math
import sys
import time

try:
    from . import object_protocol as wire
    from .windows_worker import create_engine, configure_engine
except ImportError:
    import object_protocol as wire
    from windows_worker import create_engine, configure_engine


def bypass_stereo(objects):
    """Deterministic non-Dolby fallback when DSP is disabled mid-stream.

    Native left/right channels go to their side. Other static bed channels use
    a conventional stereo sum (center/surround -3 dB, LFE -6 dB). Dynamic XYZ
    uses horizontal equal-power panning. Height/distance effects are absent.
    This is an explicit fallback mix, not a Dolby spatial renderer.
    """
    left_types = {2, 32, 128, 512, 2048, 8192, 32768}
    right_types = {4, 64, 256, 1024, 4096, 16384, 65536}
    result = [0.0] * (wire.BLOCK_FRAMES * 2)
    for item in objects:
        kind, gain = item['type'], item['gain']
        if kind == 1:
            x, _, z = item['position']
            radius = math.hypot(x, z)
            pan = x / radius if radius else 0.0
            left, right = math.sqrt((1 - pan) / 2), math.sqrt((1 + pan) / 2)
        elif kind in left_types:
            left, right = (1.0 if kind == 2 else math.sqrt(0.5)), 0.0
        elif kind in right_types:
            left, right = 0.0, (1.0 if kind == 4 else math.sqrt(0.5))
        else:
            left = right = 0.5 if kind == 16 else math.sqrt(0.5)
        for index, sample in enumerate(item['samples']):
            result[index * 2] += sample * gain * left
            result[index * 2 + 1] += sample * gain * right
    # Keep finite float32 output and match a physical stereo output's range.
    output = array.array('f', (max(-1.0, min(1.0, v)) for v in result))
    if sys.byteorder != 'little':
        output.byteswap()
    return output.tobytes()


class ObjectBatchEngine:
    block_frames, sample_rate, output_channels = wire.BLOCK_FRAMES, wire.SAMPLE_RATE, 2

    def __init__(self, namespace, *, factory=None, configure=None):
        if namespace.engine not in ('hrtf-speakers', 'hrtf-headphones') or not namespace.profile_catalog:
            raise ValueError('Object beta requires the verified full frontend and private profile catalog')
        self._configure = configure or configure_engine
        self.engine = (factory or create_engine)(namespace, object_mapper=True)
        self.closed = False
        self.bypass = bool(getattr(namespace, 'object_bypass', False))
        self.controls = {}
        self.blocks = self.original_blocks = self.bypass_blocks = self.source_blocks = 0
        self.processing_ns = self.max_processing_ns = 0
        self.last_configuration = None
        self.metadata = dict(self.engine.metadata, schema='original-dll-object-engine-v1',
                             object_protocol_version=wire.VERSION, source_format='id-type-xyz-gain-mono-float32le',
                             block_frames=wire.BLOCK_FRAMES, sample_rate=wire.SAMPLE_RATE,
                             output_channels=2, max_objects=wire.BETA_SOURCE_BUDGET,
                             beta_source_budget=wire.BETA_SOURCE_BUDGET,
                             original_dynamic_capacity=wire.INTERNAL_DYNAMIC_CAPACITY,
                             original_source_capacity=wire.INTERNAL_SOURCE_CAPACITY,
                             original_mapper=True, one_encode_per_batch=True,
                             encode_batch_frames=wire.BLOCK_FRAMES, transport_batch_frames=list(wire.BATCH_FRAMES),
                             post_stages_per_batch=1 if namespace.engine == 'hrtf-speakers' else 0,
                             bypass=self.bypass, bypass_engine='deterministic-stereo-fallback-v1',
                             reset_requires_process_replacement=True,
                             scope='Explicit game-produced source objects, maximum 32 simultaneous sources including the static bed. Dolby processing uses the same verified product profile and route-specific post stage; full Windows Atmos endpoint equivalence is unverified.')

    def configuration(self):
        profile = self.engine.control_profile
        return {'schema': 'ally-dsp-original-controls-v1', 'controls': dict(self.controls),
                'preset': profile['preset'], 'voicing': profile['voicing'], 'bypass': self.bypass}

    def configure(self, request):
        if self.closed:
            raise RuntimeError('Object engine is closed')
        if not isinstance(request, dict) or type(request.get('bypass', self.bypass)) is not bool:
            raise ValueError('Object bypass must be a boolean')
        # A configuration reset supplies controls={} and replays every official
        # profile field, just as the PCM worker does; no empty ASAR delta.
        receipt = self._configure(self.engine, request)
        self.controls = receipt['controls']
        self.bypass = request.get('bypass', self.bypass)
        self.metadata['bypass'] = self.bypass
        self.last_configuration = dict(receipt, bypass=self.bypass,
                                       audio_dolby_calls_when_bypassed=0,
                                       dsp_state_frozen_during_bypass=True)
        return self.last_configuration

    def process(self, payload, frames, count):
        if self.closed:
            raise RuntimeError('Object engine is closed')
        objects = wire.decode_objects(payload, frames, count)
        return self._render_objects(objects)

    def process_batch(self, payload, frames, count):
        if self.closed:
            raise RuntimeError('Object engine is closed')
        sources = wire.decode_batch(payload, frames, count)
        output = bytearray()
        for begin in range(0, frames, wire.BLOCK_FRAMES):
            objects = [dict(item, samples=item['samples'][begin:begin + wire.BLOCK_FRAMES]) for item in sources]
            output.extend(self._render_objects(objects))
        return bytes(output)

    def _render_objects(self, objects):
        started = time.perf_counter_ns()
        if self.bypass:
            output = bypass_stereo(objects)
            self.bypass_blocks += 1
        else:
            output = self.engine.process_objects(objects)
            self.original_blocks += 1
        elapsed = time.perf_counter_ns() - started
        if len(output) != wire.BLOCK_FRAMES * 2 * 4:
            raise RuntimeError('Object engine output format differs')
        self.processing_ns += elapsed
        self.max_processing_ns = max(self.max_processing_ns, elapsed)
        self.blocks += 1
        self.source_blocks += len(objects)
        return output

    def close(self):
        if self.closed:
            raise RuntimeError('Object engine is already closed')
        try:
            summary = self.engine.close()
        finally:
            self.closed = True
        return dict(summary, schema='original-dll-object-engine-summary-v1',
                    object_blocks=self.blocks, original_object_blocks=self.original_blocks,
                    bypass_blocks=self.bypass_blocks, source_blocks=self.source_blocks,
                    object_processing_seconds=self.processing_ns / 1e9,
                    max_object_block_seconds=self.max_processing_ns / 1e9,
                    configuration=self.configuration(), last_configuration=self.last_configuration)

    def reset(self):
        configuration = self.configuration()
        return {'schema': 'original-dll-object-reset-v1', 'replacement_required': True,
                'reset_complete': False, 'configuration': configuration,
                'reason': 'Original frontend lifetime is scoped to its dedicated worker process; replace it before acknowledging reset to the producer.',
                'summary': self.close()}
