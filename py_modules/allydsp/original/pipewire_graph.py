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
        self.created_link_objects = []
        self.reconnections = []

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

    @staticmethod
    def _matches(expected, actual):
        return all(str(actual.get(side + suffix)) == str(expected.get(side + suffix))
                   for side in ('output', 'input') for suffix in
                   ('_node', '_node_id', '_node_serial', '_port', '_port_serial', '_port_name', '_channel'))

    def _observed_links(self, dump):
        serials = {item['id']: item.get('info', {}).get('props', {}).get('object.serial')
                   for item in dump if item.get('type') == 'PipeWire:Interface:Link'}
        return [dict(route, serial=serials[route['id']]) for route in self.audit([], dump=dump)['links']]

    def _disconnect_link_objects(self, owned):
        results = []
        for saved in reversed(owned):
            current = next((item for item in self._observed_links(self.dump()) if item['id'] == saved['id']), None)
            # A port can be replaced while a surviving link is retargeted to
            # its recycled ID. Own the Link object itself, never that port ID.
            keys = ('serial', 'output_node_id', 'output_node_serial', 'input_node_id', 'input_node_serial')
            if current is None or any(current.get(key) is None or str(current[key]) != str(saved[key]) for key in keys):
                results.append({'id': saved['id'], 'serial': saved['serial'], 'skipped': 'Owned link no longer exists'})
                continue
            command(['pw-link', '--remote=' + self.remote, '--disconnect', str(saved['id'])], timeout=5)
            results.append({'id': saved['id'], 'serial': saved['serial'], 'disconnected': True})
        return results

    def connect(self, output_node, input_node, channels, timeout=5):
        if not channels or len(channels) != len(set(channels)):
            raise ValueError('Explicit route channels must be nonempty and unique')
        deadline = time.monotonic() + timeout
        initial_nodes = None
        last_error = 'Ports did not stabilize'
        for attempt in range(8):
            created, observed = [], []
            try:
                for channel in channels:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('Explicit route port negotiation exceeded its deadline')
                    item = self._connect_channel(output_node, input_node, channel, remaining, observed)
                    nodes = tuple(item[side + suffix] for side in ('output', 'input')
                                  for suffix in ('_node_id', '_node_serial'))
                    if initial_nodes is None:
                        initial_nodes = nodes
                    elif nodes != initial_nodes:
                        raise RuntimeError('Explicit route node identity changed during setup')
                    created.append(item)
                    actual = self._observed_links(self.dump())
                    if any(sum(self._matches(expected, route) for route in actual) != 1 for expected in created):
                        last_error = 'Adapter ports changed identity or channel during format negotiation'
                        break
                else:
                    self.links.extend(created)
                    return created
            except Exception:
                raise
            cleanup = self._disconnect_link_objects(observed)
            self.reconnections.append({'attempt': attempt + 1, 'reason': last_error, 'cleanup': cleanup})
            if time.monotonic() >= deadline:
                break
        raise TimeoutError('Explicit route could not stabilize after bounded reconnection: ' + last_error)

    def _connect_channel(self, output_node, input_node, channel, timeout, observed):
        # Linking may negotiate/recreate adapter ports. Resolve each pair anew
        # and never hand an earlier snapshot's numeric IDs to pw-link.
        deadline = time.monotonic() + timeout
        self.wait_ports([(output_node, channel, 'output'), (input_node, channel, 'input')], timeout=timeout)
        dump = self.dump()
        item = {'channel': channel}
        selectors = []
        for side, name, direction in [('output', output_node, 'output'), ('input', input_node, 'input')]:
            node = self.node(name, dump)
            port = self.port(name, channel, direction, dump)
            props = port['info']['props']
            port_name = props.get('port.name')
            node_serial = node['info']['props'].get('object.serial')
            port_serial = props.get('object.serial')
            if not isinstance(port_name, str) or not port_name or node_serial is None or port_serial is None:
                raise RuntimeError('Explicit route endpoint lacks a name or stable serial')
            item.update({side + '_node': name, side + '_node_id': node['id'],
                         side + '_node_serial': node_serial, side + '_port': port['id'],
                         side + '_port_serial': port_serial, side + '_port_name': port_name,
                         side + '_channel': channel})
            selectors.append(name + ':' + port_name)
        before = {(link['id'], str(link['serial'])) for link in self._observed_links(dump)}
        try:
            command(['pw-link', '--remote=' + self.remote, *selectors],
                    timeout=max(.01, deadline - time.monotonic()))
        finally:
            # pw-link waits for a definitive link state, but registry publication
            # can lag. Keep the Link object's serial for safe rollback even if
            # format negotiation has replaced its port objects in the meantime.
            while True:
                after = self._observed_links(self.dump())
                added = [link for link in after if (link['id'], str(link['serial'])) not in before
                         and all(str(link.get(side + '_node_id')) == str(item[side + '_node_id'])
                                 for side in ('output', 'input'))]
                if added or time.monotonic() >= deadline:
                    break
                time.sleep(min(.05, max(0, deadline - time.monotonic())))
            if any(link['serial'] is None for link in added):
                raise RuntimeError('New explicit link lacks stable object serial')
            observed.extend(added)
            self.created_link_objects.extend(added)
        if not added:
            raise TimeoutError('Created explicit link was not published before its deadline')
        return item

    def audit(self, allowed_nodes, dump=None):
        dump = self.dump() if dump is None else dump
        nodes = {x['id']: x
                 for x in dump if x.get('type') == 'PipeWire:Interface:Node'}
        ports = {x['id']: x for x in dump if x.get('type') == 'PipeWire:Interface:Port'}
        links = []
        for item in dump:
            if item.get('type') != 'PipeWire:Interface:Link':
                continue
            info = item['info']
            route = {'id': item['id'], 'state': info.get('state'), 'error': info.get('error')}
            for side in ('output', 'input'):
                node_id, port_id = info[side + '-node-id'], info[side + '-port-id']
                node_props = nodes.get(node_id, {}).get('info', {}).get('props', {})
                port_props = ports.get(port_id, {}).get('info', {}).get('props', {})
                route.update({side + '_node': node_props.get('node.name'), side + '_node_id': node_id,
                              side + '_node_serial': node_props.get('object.serial'), side + '_port': port_id,
                              side + '_port_serial': port_props.get('object.serial'),
                              side + '_port_name': port_props.get('port.name'),
                              side + '_channel': port_props.get('audio.channel'),
                              side + '_port_node_id': port_props.get('node.id'),
                              side + '_direction': ports.get(port_id, {}).get('info', {}).get('direction')})
            links.append(route)
        return {'nodes': [{'id': k, 'name': v.get('info', {}).get('props', {}).get('node.name')}
                          for k, v in nodes.items()], 'links': links,
                'all_links_within_allowed_nodes': all(x['output_node'] in allowed_nodes
                                                      and x['input_node'] in allowed_nodes for x in links),
                'all_links_without_error': all(not x['error'] for x in links), 'raw': dump}

    def disconnect_created(self):
        if self.created_link_objects:
            try:
                return self._disconnect_link_objects(self.created_link_objects)
            finally:
                self.created_link_objects.clear()
                self.links.clear()
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
