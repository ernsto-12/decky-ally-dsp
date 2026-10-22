"""Linux framed-PCM client, independent of the private engine implementation.

One worker process remains alive across all blocks. Calls exchange one fixed
block; PipeWire lifecycle, capture and playback belong to a separate bridge.
"""
import json
import os
import selectors
import signal
import subprocess
import time

from protocol import HEADER, READY, PROCESS, CONFIGURE, QUIT, ERROR, encode, decode_header, json_bytes


class StreamWorker:
    def __init__(self, command, *, env, cwd, stderr, startup_timeout=60, block_timeout=3):
        self.block_timeout = block_timeout
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=stderr, bufsize=0,
                                        start_new_session=True)
        self._input, self._output = self.process.stdin.fileno(), self.process.stdout.fileno()
        os.set_blocking(self._input, False)
        os.set_blocking(self._output, False)
        self.sequence = 0
        self.closed = False
        try:
            header, payload = self._read_packet(startup_timeout)
            if header['opcode'] != READY or header['sequence'] != 0:
                raise RuntimeError('Expected initial READY packet')
            self.metadata = json.loads(payload)
            self.frames = self.metadata['block_frames']
            self.input_channels = self.metadata['input_channels']
            self.output_channels = self.metadata['output_channels']
            self.sample_rate = self.metadata['sample_rate']
        except BaseException:
            self.abort()
            raise

    def _io(self, fd, size_or_payload, deadline, *, write=False):
        result = bytearray()
        view = memoryview(size_or_payload) if write else None
        expected = len(view) if write else size_or_payload
        offset = 0
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_WRITE if write else selectors.EVENT_READ)
            while offset < expected:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError('PCM worker pipe deadline exceeded')
                try:
                    if write:
                        count = os.write(fd, view[offset:])
                    else:
                        data = os.read(fd, expected - offset)
                        if not data:
                            raise EOFError('PCM worker pipe closed')
                        result.extend(data)
                        count = len(data)
                except BlockingIOError:
                    continue
                offset += count
        return None if write else bytes(result)

    def _read_packet(self, timeout):
        deadline = time.monotonic() + timeout
        raw = self._io(self._output, HEADER.size, deadline)
        header = decode_header(raw, response=True)
        payload = self._io(self._output, header['payload_bytes'], deadline)
        if header['opcode'] == ERROR:
            raise RuntimeError('Original DLL worker: ' + payload.decode('utf-8', errors='replace'))
        return header, payload

    def exchange(self, payload):
        if self.closed or len(payload) != self.frames * self.input_channels * 4:
            raise ValueError('Expected one negotiated input block')
        self.sequence += 1
        self._io(self._input, encode(PROCESS, self.sequence, self.frames,
                                    self.input_channels, payload),
                 time.monotonic() + self.block_timeout, write=True)
        header, output = self._read_packet(self.block_timeout)
        if (header['opcode'] != PROCESS or header['sequence'] != self.sequence
                or header['frames'] != self.frames or header['channels'] != self.output_channels
                or len(output) != self.frames * self.output_channels * 4):
            raise RuntimeError('PCM response differs from negotiated block/sequence')
        return output

    def configure(self, controls, *, preset=None, voicing=None):
        if self.closed or self.metadata.get('runtime_controls') is not True:
            raise RuntimeError('This imported engine does not support runtime controls')
        request = {'schema': 'ally-dsp-original-controls-v1', 'controls': controls}
        if preset is not None:
            request['preset'] = preset
        if voicing is not None:
            request['voicing'] = voicing
        payload = json_bytes(request)
        self.sequence += 1
        self._io(self._input, encode(CONFIGURE, self.sequence, payload=payload),
                 time.monotonic() + self.block_timeout, write=True)
        header, raw = self._read_packet(self.block_timeout)
        if (header['opcode'] != CONFIGURE or header['sequence'] != self.sequence
                or header['frames'] or header['channels']):
            raise RuntimeError('Control response differs from the request')
        result = json.loads(raw)
        if result.get('applied') is not True:
            raise RuntimeError('Original receiver did not apply the control update')
        return result

    def close(self):
        if self.closed:
            raise RuntimeError('Worker already closed')
        self.sequence += 1
        self._io(self._input, encode(QUIT, self.sequence),
                 time.monotonic() + self.block_timeout, write=True)
        header, payload = self._read_packet(self.block_timeout)
        if header['opcode'] != QUIT or header['sequence'] != self.sequence:
            raise RuntimeError('Expected clean QUIT response')
        self.process.stdin.close()
        self.process.stdout.close()
        code = self.process.wait(timeout=20)
        self.closed = True
        if code:
            raise RuntimeError('Worker exited nonzero after QUIT: ' + str(code))
        return json.loads(payload)

    def abort(self):
        # Only the process session that this object created may be signalled.
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=20)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()
        self.closed = True
