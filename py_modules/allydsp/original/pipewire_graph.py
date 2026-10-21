"""Small explicit PipeWire graph operations; no default or policy changes."""
import json
import subprocess
import time


def command(argv, *, timeout=8):
    run = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if run.returncode:
        raise RuntimeError('PipeWire command failed: ' + repr(argv) + ': ' + run.stderr[-1000:])
    return run.stdout


class Graph:
    def __init__(self, remote):
        self.remote = remote
        self.links = []

    def dump(self):
        return json.loads(command(['pw-dump', '--remote', self.remote]))

    def node(self, name, dump=None):
        matches = [x for x in (self.dump() if dump is None else dump)
                   if x.get('type') == 'PipeWire:Interface:Node'
                   and x.get('info', {}).get('props', {}).get('node.name') == name]
        if len(matches) != 1:
            raise LookupError('Expected one named node: ' + name)
        return matches[0]

    def port(self, node_name, channel, direction, dump=None):
        dump = self.dump() if dump is None else dump
        node = self.node(node_name, dump)
        ports = [x for x in dump if x.get('type') == 'PipeWire:Interface:Port'
                 and str(x.get('info', {}).get('props', {}).get('node.id')) == str(node['id'])
                 and x.get('info', {}).get('direction') == direction
                 and x.get('info', {}).get('props', {}).get('audio.channel') == channel]
        if len(ports) != 1:
            raise LookupError('Expected one port: ' + repr((node_name, channel, direction)))
        return ports[0]

    def wait_ports(self, pairs, timeout=12):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            dump = self.dump()
            try:
                return [self.port(*item, dump=dump) for item in pairs]
            except LookupError as error:
                last = error
                time.sleep(.05)
        raise TimeoutError('PipeWire ports unavailable: ' + str(last))

    def connect(self, output_node, input_node, channels):
        ports = self.wait_ports([(node, channel, direction)
                                 for channel in channels
                                 for node, direction in [(output_node, 'output'), (input_node, 'input')]])
        created = []
        for index, channel in enumerate(channels):
            output, input_ = ports[index * 2:index * 2 + 2]
            command(['pw-link', '--remote=' + self.remote, str(output['id']), str(input_['id'])])
            item = {'output_node': output_node, 'input_node': input_node, 'channel': channel,
                    'output_port': output['id'], 'input_port': input_['id'],
                    'output_port_serial': output['info']['props'].get('object.serial'),
                    'input_port_serial': input_['info']['props'].get('object.serial')}
            self.links.append(item)
            created.append(item)
        return created

    def audit(self, allowed_nodes):
        dump = self.dump()
        nodes = {x['id']: x.get('info', {}).get('props', {}).get('node.name')
                 for x in dump if x.get('type') == 'PipeWire:Interface:Node'}
        links = []
        for item in dump:
            if item.get('type') != 'PipeWire:Interface:Link':
                continue
            info = item['info']
            links.append({'id': item['id'], 'output_node': nodes.get(info['output-node-id']),
                          'input_node': nodes.get(info['input-node-id']),
                          'state': info.get('state'), 'error': info.get('error')})
        return {'nodes': [{'id': k, 'name': v} for k, v in nodes.items()], 'links': links,
                'all_links_within_allowed_nodes': all(x['output_node'] in allowed_nodes
                                                      and x['input_node'] in allowed_nodes for x in links),
                'all_links_without_error': all(not x['error'] for x in links), 'raw': dump}

    def disconnect_created(self):
        results = []
        for item in reversed(self.links):
            try:
                # Global IDs can be recycled after a child exits. Never unlink
                # a pair unless both current ports still have the saved serial.
                dump = {x['id']: x for x in self.dump()}
                still_owned = all(port in dump and dump[port].get('type') == 'PipeWire:Interface:Port'
                                  and dump[port]['info']['props'].get('object.serial') == item[which + '_port_serial']
                                  for which, port in [('output', item['output_port']), ('input', item['input_port'])])
                if not still_owned:
                    results.append(dict(item, skipped='Original port no longer exists'))
                    continue
                run = subprocess.run(['pw-link', '--remote=' + self.remote, '--disconnect',
                                      str(item['output_port']), str(item['input_port'])],
                                     capture_output=True, text=True, timeout=5)
                results.append(dict(item, returncode=run.returncode, stderr=run.stderr))
            except Exception as error:
                results.append(dict(item, cleanup_error=str(error)))
        self.links.clear()
        return results
