"""Explicit monitor -> persistent DLL worker -> playback PipeWire bridge.

No default sink/source is read or changed. Capture always uses sink monitors,
all stream nodes start with autoconnect disabled, and each link is explicit.
The JSON config supplies locally provisioned worker paths; no vendor assets are
embedded. This process does not create or change systemd units or plugins.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import selectors
import signal
import subprocess
import threading
import time

from linux_client import StreamWorker
from pipewire_graph import Graph


def write_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temp.replace(path)


def stop_child(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return process.wait(timeout=3)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
    return process.returncode


def pcm_command(remote, name, channels, positions, *, capture=False, latency=256):
    props = {'node.name': name, 'node.autoconnect': False, 'node.dont-reconnect': True,
             'state.restore-props': False, 'state.restore-target': False,
             'resample.disable': True, 'channelmix.disable': True,
             'adapter.auto-port-config': {'mode': 'dsp', 'position': 'preserve'}}
    if capture:
        props['stream.capture.sink'] = True
    return ['pw-cat', '--remote', remote, '--record' if capture else '--playback', '--raw',
            '--format', 'f32', '--rate', '48000', '--channels', str(channels),
            '--channel-map', ','.join(positions), '--latency', str(latency), '--target', '0',
            '--properties', json.dumps(props), '-']


def wait_for_explicit_routes(graph, allowed, capture_sink, capture_name, playback_name,
                             playback_sink, expected_routes, stop, shutdown, observations,
                             timeout=5):
    """Wait for publication, then pin every channel and both endpoint identities."""
    began = time.monotonic()
    deadline = began + timeout
    expected_pairs = ((capture_sink, capture_name), (playback_name, playback_sink))
    expected_count = len(expected_routes)
    route_keys = [(item['output_node'], item['input_node'], item['channel']) for item in expected_routes]
    if not expected_count or len(set(route_keys)) != expected_count:
        raise ValueError('Explicit route plan must contain unique channel pairs')
    if any(key[:2] not in expected_pairs for key in route_keys):
        raise ValueError('Explicit route plan contains an unexpected node pair')
    identity_keys = tuple(side + suffix for side in ('output', 'input') for suffix in
                          ('_node', '_node_id', '_node_serial', '_port', '_port_serial', '_port_name', '_channel'))
    if any(item.get(key) is None for item in expected_routes for key in identity_keys):
        raise ValueError('Explicit route plan lacks endpoint identity')
    own_ids = {item[side + '_node_id'] for item in expected_routes for side in ('output', 'input')
               if item[side + '_node'] in (capture_name, playback_name)}
    while True:
        if stop.is_set() and shutdown['signal'] is not None:
            return None
        snapshot = graph.audit(allowed)
        own = [link for link in snapshot['links']
               if link['output_node'] in (capture_name, playback_name)
               or link['input_node'] in (capture_name, playback_name)
               or link.get('output_node_id') in own_ids or link.get('input_node_id') in own_ids]
        if len(own) > expected_count:
            raise RuntimeError('Explicit bridge route has duplicate links')
        if any(link['error'] or link.get('state') == 'error' for link in own):
            raise RuntimeError('Explicit bridge route has a failed link')
        pending = any(link.get('state') not in ('active', 'paused') for link in own)
        matched = set()
        for link in own:
            # Registry objects may be published after the link itself. Missing
            # endpoint metadata can wait; conflicting metadata must fail closed.
            complete = all(link.get(key) is not None for key in identity_keys)
            candidates = [index for index, expected in enumerate(expected_routes)
                          if all(link.get(key) is None or str(link[key]) == str(expected[key])
                                 for key in identity_keys)]
            if not candidates:
                raise RuntimeError('Explicit bridge route endpoint identity or channel mismatch')
            for side in ('output', 'input'):
                if ((link.get(side + '_port_node_id') is not None
                     and str(link[side + '_port_node_id']) != str(link[side + '_node_id']))
                    or (link.get(side + '_direction') is not None and link[side + '_direction'] != side)):
                    raise RuntimeError('Explicit bridge route port ownership or direction mismatch')
            if not complete or any(link.get(side + '_port_node_id') is None
                                   or link.get(side + '_direction') is None for side in ('output', 'input')):
                pending = True
                continue
            if len(candidates) != 1:
                raise RuntimeError('Explicit bridge route endpoint matches multiple requested channels')
            if candidates[0] in matched:
                raise RuntimeError('Explicit bridge route has duplicate channel links')
            matched.add(candidates[0])
        if len(observations) < 128:
            observations.append({'elapsed_seconds': time.monotonic() - began,
                                 'owned_link_count': len(own), 'verified_channel_count': len(matched),
                                 'pending': pending})
        if len(own) == expected_count and len(matched) == expected_count and not pending:
            return snapshot
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Explicit bridge route incomplete after bounded wait: '
                               + str(len(own)) + '/' + str(expected_count) + ' links')
        stop.wait(min(.05, remaining))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', type=Path, required=True)
    ns = ap.parse_args()
    config = json.loads(ns.config.read_text())
    if config.get('schema') != 'original-dll-pipewire-bridge-v1':
        raise ValueError('Unknown bridge configuration version')
    namespace = config['namespace']
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,55}', namespace):
        raise ValueError('Invalid explicit node namespace')
    remote, capture_sink, playback_sink = (config[x] for x in ('remote', 'capture_sink', 'playback_sink'))
    if any(not isinstance(x, str) or not x or x.startswith('@DEFAULT') for x in (remote, capture_sink, playback_sink)):
        raise ValueError('Explicit PipeWire remote and target names are required')
    if capture_sink == playback_sink:
        raise ValueError('Input and output targets must differ')
    positions = config['input_positions']
    if not positions or len(positions) != len(set(positions)) or any(x not in ('FL', 'FR', 'FC', 'LFE', 'RL', 'RR', 'SL', 'SR', 'MONO') for x in positions):
        raise ValueError('Unknown input channel map')
    audit_root = Path(config['audit_directory']).resolve()
    audit_root.mkdir(parents=True, exist_ok=True)
    audit_dir = audit_root / ('run-' + str(os.getpid()) + '-' + str(time.time_ns()))
    audit_dir.mkdir(exist_ok=False)
    status_path = Path(config.get('status_path', str(audit_root / 'status.json'))).resolve()
    status_path.parent.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    shutdown = {'signal': None, 'received_monotonic': None}
    def request_stop(signum, _frame):
        shutdown['signal'] = signal.Signals(signum).name
        shutdown['received_monotonic'] = time.monotonic()
        stop.set()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_stop)
    graph = Graph(remote)
    worker = None
    capture = playback = None
    reader = setup = None
    diagnostic_files = []
    fifo = queue.Queue(maxsize=int(config.get('queue_blocks', 8)))
    if not 2 <= fifo.maxsize <= 64:
        raise ValueError('Bounded queue size must be 2..64 blocks')
    counters = {'captured_blocks': 0, 'processed_blocks': 0, 'queue_high_water_blocks': 0,
                'queue_overflows': 0, 'capture_stall_timeouts': 0, 'playback_pipe_timeouts': 0,
                'worker_deadline_timeouts': 0, 'capture_eof': False}
    audit = {'schema': 'original-dll-pipewire-bridge-execution-v1', 'namespace': namespace,
             'remote': remote, 'capture_sink': capture_sink, 'playback_sink': playback_sink,
             'config_sha256': hashlib.sha256(ns.config.read_bytes()).hexdigest(),
             'counters': counters, 'opened_microphone': False, 'default_audio_changed': False,
             'queue_capacity_blocks': fifo.maxsize, 'started_monotonic': time.monotonic()}
    reader_errors = []
    roundtrip_count = roundtrip_total_ns = roundtrip_max_ns = 0
    input_digest, output_digest = hashlib.sha256(), hashlib.sha256()
    input_bytes = output_bytes = 0
    error = None
    clean = False
    io_stopped = False
    ready = threading.Event()
    control_state = {'request_id': None, 'result': None, 'error': None}
    controls_file = Path(config['controls_file']) if config.get('controls_file') else None
    control_identity = None
    last_control_check = 0.0

    def apply_control_file():
        nonlocal control_identity
        if controls_file is None:
            return
        try:
            info = controls_file.lstat()
            identity = (info.st_ino, info.st_mtime_ns, info.st_size)
            if identity == control_identity:
                return
            if controls_file.is_symlink() or not controls_file.is_file() or info.st_size > 32768:
                raise ValueError('Expected a bounded regular control request')
            raw = controls_file.read_bytes()
            if len(raw) > 32768:
                raise ValueError('Control request exceeds its limit')
            request = json.loads(raw)
            if (request.get('schema') != 'ally-dsp-original-control-request-v1'
                    or not re.fullmatch(r'[0-9a-f]{32}', str(request.get('id', '')))):
                raise ValueError('Invalid control request identity')
        except (OSError, ValueError, TypeError) as caught:
            control_state['error'] = str(caught)
            return
        # Runtime validation happens before the original receiver is called.
        # A receiver/transport failure terminates this bridge and restores the
        # bypass route instead of continuing with an uncertain partial update.
        result = worker.configure(request['controls'], preset=request.get('preset'), voicing=request.get('voicing'))
        control_identity = identity
        control_state.update(request_id=request['id'], result=result, error=None)
    capture_name, playback_name = namespace + '.capture', namespace + '.playback'
    def publish_status(state, *, last_error=None):
        metadata = worker.metadata if worker is not None else {}
        write_json(status_path, {
            'schema': 'original-dll-pipewire-bridge-status-v1', 'state': state,
            'ready': state == 'ready', 'node_routes_verified': ready.is_set(),
            'worker_pid': worker.process.pid if worker is not None else None,
            'daemon_pid': os.getpid(), 'owned_nodes': [capture_name, playback_name],
            'engine': metadata.get('engine'), 'capture_sink': capture_sink, 'playback_sink': playback_sink,
            'sample_rate': metadata.get('sample_rate'), 'block_frames': metadata.get('block_frames'),
            'declared_dsp_latency_frames': metadata.get('declared_dsp_latency_frames'),
            'declared_dsp_latency_ms': metadata.get('declared_dsp_latency_ms'),
            'playback_requested_latency_frames': audit.get('playback_requested_latency_frames'),
            'playback_silent_prefill_frames': audit.get('playback_silent_prefill_frames'),
            'end_to_end_latency_measured': False,
            'counters': dict(counters), 'last_error': last_error,
            'controls': dict(control_state),
            'audit_directory': str(audit_dir), 'updated_unix_seconds': time.time()})
    publish_status('starting')
    def diagnostics(name):
        file = (audit_dir / name).open('xb')
        diagnostic_files.append(file)
        return file
    def capture_reader():
        pending = bytearray()
        fd = capture.stdout.fileno()
        os.set_blocking(fd, False)
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while not stop.is_set():
                if not selector.select(.2):
                    continue
                try:
                    piece = os.read(fd, max(65536, block_bytes))
                except BlockingIOError:
                    continue
                if not piece:
                    counters['capture_eof'] = True
                    if not stop.is_set():
                        reader_errors.append('Capture stream ended unexpectedly')
                        stop.set()
                    break
                pending.extend(piece)
                while len(pending) >= block_bytes:
                    payload = bytes(pending[:block_bytes])
                    del pending[:block_bytes]
                    try:
                        fifo.put_nowait(payload)
                    except queue.Full:
                        counters['queue_overflows'] += 1
                        reader_errors.append('Bounded PCM capture queue overflow')
                        stop.set()
                        return
                    counters['captured_blocks'] += 1
                    counters['queue_high_water_blocks'] = max(counters['queue_high_water_blocks'], fifo.qsize())
    def stop_streams():
        nonlocal io_stopped
        if io_stopped:
            return
        io_stopped = True
        stop.set()
        if reader:
            reader.join(timeout=2)
        if setup:
            setup.join(timeout=15)
        audit['owned_child_cleanup'] = {}
        # raw pw-cat can be blocked in stdio while handling SIGTERM. Closing
        # our pipe ends wakes those reads/writes before waiting for the child.
        for process in (capture, playback):
            if process is not None:
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None:
                        pipe.close()
        for name, process in [('capture', capture), ('playback', playback)]:
            if process is not None:
                audit['owned_child_cleanup'][name] = stop_child(process)
        audit['created_link_cleanup'] = graph.disconnect_created()
    try:
        for target in (capture_sink, playback_sink):
            node = graph.node(target)
            if node['info']['props'].get('media.class') != 'Audio/Sink':
                raise ValueError('Capture/playback target must be an explicitly named Audio/Sink')
        worker_cfg = config['worker']
        worker_env = dict(os.environ)
        worker_env.update(worker_cfg.get('environment', {}))
        worker = StreamWorker(worker_cfg['command'], env=worker_env,
                              cwd=worker_cfg['cwd'], stderr=diagnostics('worker.stderr.log'))
        if worker.input_channels != len(positions) or worker.output_channels != 2 or worker.sample_rate != 48000:
            raise ValueError('Worker format differs from explicit PipeWire channel map')
        audit['worker_metadata'] = worker.metadata
        if controls_file is not None:
            apply_control_file()
            if control_state['error']:
                raise ValueError(control_state['error'])
        block_bytes = worker.frames * worker.input_channels * 4
        capture_cmd = pcm_command(remote, capture_name, worker.input_channels, positions, capture=True)
        playback_latency = int(config.get('playback_latency_frames', 1024))
        if not 256 <= playback_latency <= 4096 or playback_latency % 256:
            raise ValueError('Playback latency must be a bounded multiple of 256 frames')
        playback_cmd = pcm_command(remote, playback_name, 2, ['FL', 'FR'], latency=playback_latency)
        audit['capture_command'], audit['playback_command'] = capture_cmd, playback_cmd
        capture = subprocess.Popen(capture_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=diagnostics('capture.stderr.log'), bufsize=0, start_new_session=True)
        playback = subprocess.Popen(playback_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=diagnostics('playback.stderr.log'), bufsize=0, start_new_session=True)
        # pw-cat may pre-read stdin before exposing ports. This bounded silent
        # prefill is identified in the audit and is not counted as DSP output.
        prefill_frames = 1024
        prefill = bytes(prefill_frames * 2 * 4)
        playback.stdin.write(prefill)
        playback.stdin.flush()
        audit['playback_silent_prefill_frames'] = prefill_frames
        audit['playback_requested_latency_frames'] = playback_latency
        allowed = config.get('allowed_graph_nodes', [capture_sink, playback_sink, capture_name, playback_name])
        reader = threading.Thread(target=capture_reader, name='pcm-capture', daemon=True)
        reader.start()
        def setup_graph():
            try:
                # A capture monitor starts producing as soon as its first link
                # activates. Prepare the consumer before linking that source so
                # port negotiation cannot fill the bounded PCM queue/pipe.
                graph.connect(playback_name, playback_sink, ['FL', 'FR'])
                graph.connect(capture_sink, capture_name, positions)
                audit['created_links'] = list(graph.links)
                audit['route_reconnections'] = list(graph.reconnections)
                if len(audit['created_links']) != len(positions) + 2:
                    raise RuntimeError('Explicit channel route plan is incomplete')
                audit['route_wait_observations'] = []
                snapshot = wait_for_explicit_routes(
                    graph, allowed, capture_sink, capture_name, playback_name, playback_sink,
                    audit['created_links'], stop, shutdown, audit['route_wait_observations'])
                if snapshot is None:
                    audit['route_setup_interrupted_by_signal'] = True
                    return
                audit['ready_graph'] = snapshot
                deadline = time.monotonic() + 5
                while counters['processed_blocks'] < 1:
                    if stop.is_set() or time.monotonic() >= deadline:
                        raise TimeoutError('A processed PCM block is required before READY')
                    time.sleep(.005)
                write_json(audit_dir / 'ready.json', {'ready': True, 'worker': worker.metadata,
                                                    'capture_node': capture_name, 'playback_node': playback_name})
                ready.set()
                publish_status('ready')
            except Exception as caught:
                try:
                    audit['graph_setup_failure_dump'] = graph.dump()
                except Exception:
                    pass
                reader_errors.append('Graph setup: ' + str(caught))
                stop.set()
        setup = threading.Thread(target=setup_graph, name='explicit-pipewire-links', daemon=True)
        setup.start()
        fd = playback.stdin.fileno()
        os.set_blocking(fd, False)
        began = last_progress = last_status = time.monotonic()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_WRITE)
            def write_playback(data):
                offset = 0
                deadline = time.monotonic() + .5
                while offset < len(data):
                    left = deadline - time.monotonic()
                    if left <= 0 or not selector.select(left):
                        counters['playback_pipe_timeouts'] += 1
                        raise TimeoutError('Playback PCM pipe stopped consuming data')
                    try:
                        offset += os.write(fd, data[offset:])
                    except BlockingIOError:
                        pass
            input_file = (audit_dir / 'captured-worker-input.f32').open('xb') if config.get('retain_pcm') else None
            output_file = (audit_dir / 'worker-output.f32').open('xb') if config.get('retain_pcm') else None
            try:
                while not stop.is_set():
                    duration = config.get('duration_seconds', 0)
                    if duration and time.monotonic() - began >= duration:
                        clean = True
                        break
                    now = time.monotonic()
                    if controls_file is not None and now - last_control_check >= .1:
                        previous_request = control_state['request_id']
                        apply_control_file()
                        last_control_check = now
                        if control_state['request_id'] != previous_request:
                            publish_status('ready' if ready.is_set() else 'starting')
                    try:
                        payload = fifo.get(timeout=.1 if ready.is_set() else .005)
                    except queue.Empty:
                        if capture.poll() is not None or playback.poll() is not None:
                            raise RuntimeError('Owned pw-cat process exited unexpectedly')
                        if time.monotonic() - last_progress > config.get('capture_stall_seconds', 5):
                            counters['capture_stall_timeouts'] += 1
                            raise TimeoutError('Sink monitor stopped delivering PCM')
                        if not ready.is_set():
                            # raw pw-cat reads stdin on its own main loop.
                            # Keep it supplied while ports negotiate, otherwise
                            # that read can prevent the remaining links from
                            # activating. This is bounded startup silence, not
                            # processed DLL output or captured game audio.
                            if audit.get('startup_silent_frames', 0) + worker.frames > 5 * 48000:
                                raise TimeoutError('Startup PCM silence limit reached')
                            write_playback(bytes(worker.frames * 2 * 4))
                            audit['startup_silent_frames'] = audit.get('startup_silent_frames', 0) + worker.frames
                        continue
                    tick = time.perf_counter_ns()
                    try:
                        result = worker.exchange(payload)
                    except TimeoutError:
                        counters['worker_deadline_timeouts'] += 1
                        raise
                    elapsed_ns = time.perf_counter_ns() - tick
                    roundtrip_count += 1
                    roundtrip_total_ns += elapsed_ns
                    roundtrip_max_ns = max(roundtrip_max_ns, elapsed_ns)
                    write_playback(result)
                    counters['processed_blocks'] += 1
                    input_digest.update(payload)
                    output_digest.update(result)
                    input_bytes += len(payload)
                    output_bytes += len(result)
                    if input_file:
                        input_file.write(payload)
                        output_file.write(result)
                    last_progress = time.monotonic()
                    if last_progress - last_status >= 1:
                        publish_status('ready' if ready.is_set() else 'starting')
                        last_status = last_progress
            finally:
                if input_file:
                    input_file.close()
                    output_file.close()
        if reader_errors:
            raise RuntimeError('; '.join(reader_errors))
        if stop.is_set():
            clean = True
        publish_status('stopping')
        # Remove the realtime paths before waiting for the Windows process to
        # exit. Otherwise a slow WinRT shutdown can starve still-connected nodes.
        stop_streams()
        audit['worker_summary'] = worker.close()
    except Exception as caught:
        # systemd KillMode=control-group signals our pipe peers together with
        # this daemon. A final read/write can observe their closed pipe after
        # our stop signal. Preserve actual queue/worker/graph failures.
        expected_closed_pipe = (
            shutdown['signal'] is not None and stop.is_set() and not reader_errors
            and isinstance(caught, (BrokenPipeError, EOFError))
            and not any(counters[key] for key in ('queue_overflows', 'capture_stall_timeouts',
                                                'playback_pipe_timeouts', 'worker_deadline_timeouts')))
        if expected_closed_pipe:
            clean = True
            audit['shutdown_io_event'] = {'type': type(caught).__name__, 'message': str(caught)}
        else:
            error = {'type': type(caught).__name__, 'message': str(caught)}
    finally:
        publish_status('error' if error else 'stopping', last_error=error)
        stop_streams()
        if worker is not None and not worker.closed:
            worker.abort()
        audit['worker_returncode'] = worker.process.poll() if worker is not None else None
        for file in diagnostic_files:
            file.close()
        audit.update(clean_stop=clean and error is None, error=error, reader_errors=reader_errors,
                     shutdown_signal=shutdown['signal'],
                     shutdown_signal_monotonic=shutdown['received_monotonic'],
                     shutdown_reason=('signal' if shutdown['signal'] is not None else 'duration') if clean and error is None else 'error',
                     stream_input_bytes=input_bytes, stream_output_bytes=output_bytes,
                     stream_input_sha256=input_digest.hexdigest(), stream_output_sha256=output_digest.hexdigest(),
                     roundtrip_nanoseconds={'count': roundtrip_count, 'mean': roundtrip_total_ns / roundtrip_count if roundtrip_count else None,
                                            'max': roundtrip_max_ns},
                     elapsed_seconds=time.monotonic() - audit['started_monotonic'])
        write_json(audit_dir / 'bridge.json', audit)
        publish_status('error' if error else 'stopped', last_error=error)
    print(json.dumps({'clean_stop': audit['clean_stop'], 'error': error, 'counters': counters}), flush=True)
    return 0 if audit['clean_stop'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
