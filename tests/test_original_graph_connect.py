"""Vendor-free simulation of PipeWire's first-link port recreation."""
import copy
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def module():
    source = Path(__file__).resolve().parents[1] / 'py_modules/allydsp/original/pipewire_graph.py'
    spec = importlib.util.spec_from_file_location('_allydsp_graph_connect_test', source)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


CHANNELS = ['FL', 'FR', 'FC', 'LFE', 'RL', 'RR']


class Registry:
    def __init__(self, module, monkeypatch, recreate_at=None, always_recreate=False):
        self.module = module
        self.recreate_at = recreate_at
        self.always_recreate = always_recreate
        self.creates = 0
        self.commands = []
        self.generation = 0
        self.next_link = 300
        self.objects = [self.node(10, 'source', 1010), self.node(20, 'capture', 1020)]
        self.objects += [self.port(100 + i, 10, channel, i, 'output', 1000 + i)
                         for i, channel in enumerate(CHANNELS)]
        self.objects += [self.port(200 + i, 20, channel, i, 'input', 2000 + i)
                         for i, channel in enumerate(CHANNELS)]
        monkeypatch.setattr(module, 'command', self.command)
        self.graph = module.Graph('test-only')
        monkeypatch.setattr(self.graph, 'dump', lambda: copy.deepcopy(self.objects))

    @staticmethod
    def node(id_, name, serial):
        return {'id': id_, 'type': 'PipeWire:Interface:Node',
                'info': {'props': {'node.name': name, 'object.serial': serial}}}

    @staticmethod
    def port(id_, node, channel, index, direction, serial):
        return {'id': id_, 'type': 'PipeWire:Interface:Port',
                'info': {'direction': direction, 'props': {'node.id': node, 'object.serial': serial,
                         'port.name': ('monitor_' if direction == 'output' else 'input_') + channel,
                         'port.id': index, 'audio.channel': channel}}}

    def resolve(self, selector):
        name, port = selector.split(':', 1)
        return next(item['id'] for item in self.objects if item['type'] == 'PipeWire:Interface:Port'
                    and item['info']['props']['node.id'] == (10 if name == 'source' else 20)
                    and item['info']['props']['port.name'] == port)

    def command(self, args, **_kwargs):
        assert args[:2] == ['pw-link', '--remote=test-only']
        self.commands.append(args)
        if args[2] == '--disconnect':
            assert len(args) == 4  # Single global Link ID, never recycled Port IDs.
            self.objects = [item for item in self.objects if item['id'] != int(args[3])]
            return ''
        output, input_ = map(self.resolve, args[2:])
        self.creates += 1
        if self.always_recreate or self.creates == self.recreate_at:
            self.generation += 1
            self.objects = [item for item in self.objects
                            if not (item['type'] == 'PipeWire:Interface:Port'
                                    and item['info']['props']['node.id'] == 20)]
            self.objects += [self.port(200 + i, 20, channel, i, 'input', 2000 + 100*self.generation + i)
                             for i, channel in enumerate(reversed(CHANNELS) if self.generation % 2 else CHANNELS)]
        self.objects.append({'id': self.next_link, 'type': 'PipeWire:Interface:Link',
                             'info': {'props': {'object.serial': 10000+self.next_link},
                                      'output-node-id': 10, 'input-node-id': 20,
                                      'output-port-id': output, 'input-port-id': input_, 'state': 'active'}})
        self.next_link += 1
        return ''


@pytest.mark.parametrize('recreate_at', [None, 1, 3], ids=['stable', 'first-format-negotiation', 'later-negotiation'])
def test_named_connect_retries_recycled_port_ids_and_verifies_all_channels(module, monkeypatch, recreate_at):
    registry = Registry(module, monkeypatch, recreate_at=recreate_at)
    plan = registry.graph.connect('source', 'capture', CHANNELS)
    actual = registry.graph.audit([])['links']
    assert len(plan) == len(actual) == 6
    assert all(sum(registry.graph._matches(expected, route) for route in actual) == 1 for expected in plan)
    assert all(route['output_channel'] == route['input_channel'] for route in actual)
    assert all('source:monitor_' in args[2] and 'capture:input_' in args[3]
               for args in registry.commands if args[2] != '--disconnect')
    assert bool(registry.graph.reconnections) is bool(recreate_at)
    cleanup = registry.graph.disconnect_created()
    assert any(item.get('disconnected') for item in cleanup)
    assert registry.graph.audit([])['links'] == []


def test_permanently_recreated_ports_fail_after_finite_retries(module, monkeypatch):
    registry = Registry(module, monkeypatch, always_recreate=True)
    with pytest.raises(TimeoutError, match='bounded reconnection'):
        registry.graph.connect('source', 'capture', CHANNELS)
    assert registry.creates == 8
    assert len(registry.graph.reconnections) == 8
    assert registry.graph.audit([])['links'] == []


def test_cleanup_never_unlinks_a_recycled_link_object(module, monkeypatch):
    registry = Registry(module, monkeypatch)
    registry.graph.connect('source', 'capture', ['FL'])
    link = next(item for item in registry.objects if item['type'] == 'PipeWire:Interface:Link')
    link['info']['props']['object.serial'] += 1
    result = registry.graph.disconnect_created()
    assert result[0]['skipped'] == 'Owned link no longer exists'
    assert registry.graph.audit([])['links']
    assert not any(args[2] == '--disconnect' for args in registry.commands)


def test_failed_link_command_still_tracks_its_created_object_for_cleanup(module, monkeypatch):
    registry = Registry(module, monkeypatch)
    original = registry.command
    def command(args, **kwargs):
        result = original(args, **kwargs)
        if args[2] != '--disconnect':
            raise RuntimeError('Client failed after server-side creation')
        return result
    monkeypatch.setattr(module, 'command', command)
    with pytest.raises(RuntimeError, match='after server-side'):
        registry.graph.connect('source', 'capture', ['FL'])
    assert registry.graph.disconnect_created()[0]['disconnected']
    assert registry.graph.audit([])['links'] == []


@pytest.mark.parametrize('channels', [[], ['FL', 'FL']])
def test_empty_or_duplicate_channel_plan_does_not_create_links(module, monkeypatch, channels):
    registry = Registry(module, monkeypatch)
    with pytest.raises(ValueError):
        registry.graph.connect('source', 'capture', channels)
    assert registry.commands == []
