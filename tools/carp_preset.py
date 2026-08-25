"""Turn CARP day assignments into the preset JSON the app already reads.

A CARP day is a list of required edges in the order they are walked.  A preset
day is every arc the hiker actually covers, connectors included, so this fills
in the approach from the morning pick-up, the shortest path between one
required edge and the next, and the walk out at night.

The schema needs nothing new.  A supported day already ends where the next one
does not begin -- that is what the crew-shuttle count reports -- so days that
never chain are a shape the map, the CSV and the validator all handle today.

Coverage is the thing to get right.  A connector may well re-walk a required
edge, and the validator wants exactly one traversal of each carrying
is_deadhead false.  Only the day's own legs are marked required here; every
arc that comes from a shortest path is deadhead, whatever trail it lies on.
"""
import networkx as nx

FERRY_NAMES = {'TI051': 'Hazel Creek Access', 'TI053': 'Ollie Cove',
               'TI064': 'Pilkey Creek', 'BC090': 'Campsite 90'}


class Expander:
    def __init__(self, net):
        self.net = net
        self._cache = {}
        self.by_id = {}
        for r in net.rows:
            self.by_id[str(float(r['ID']))] = r

    def path(self, u, v):
        """Node sequence for the cheapest walk from u to v."""
        if u == v:
            return [u]
        key = (u, v)
        if key not in self._cache:
            self._cache[key] = nx.dijkstra_path(self.net.G, u, v,
                                                weight='weight')
        return self._cache[key]

    def deadhead(self, nodes):
        """Arcs along a node sequence, all marked as covering nothing."""
        out = []
        for u, v in zip(nodes, nodes[1:]):
            a = dict(self.net.arc[(u, v)])
            a.update({'from': u, 'to': v, 'is_deadhead': True,
                      'direction': 'dh'})
            out.append({'from': u, 'to': v, 'edge_id': a['edge_id'],
                        'trail': a['trail'], 'miles': a['miles'],
                        'seconds': a['seconds'], 'gain': a['gain'],
                        'is_deadhead': True, 'direction': 'dh'})
        return out

    def leg(self, item):
        """The one arc that covers a required edge, in the chosen direction."""
        eid, d = item
        r = self.by_id[eid]
        a, b = r['node_A'], r['node_B']
        u, v, cost, gain, name = ((a, b, 'cost_A_to_B', 'gain_A_to_B', 'fwd')
                                  if d == 0 else
                                  (b, a, 'cost_B_to_A', 'gain_B_to_A', 'rev'))
        return {'from': u, 'to': v, 'edge_id': eid, 'trail': r['Trail'],
                'miles': float(r['Miles']), 'seconds': int(r[cost]),
                'gain': int(float(r[gain] or 0)), 'is_deadhead': False,
                'direction': name}

    def day(self, route):
        """Every arc of one day, pick-up to pick-up."""
        net = self.net
        first, last = net.leg(route[0])[0], net.leg(route[-1])[1]
        arcs = self.deadhead(net.from_path[first])
        for k, item in enumerate(route):
            arcs.append(self.leg(item))
            if k + 1 < len(route):
                arcs += self.deadhead(
                    self.path(net.leg(item)[1], net.leg(route[k + 1])[0]))
        arcs += self.deadhead(net.to_path[last])
        return arcs


def build_preset(net, days, budget, total_required_miles=None):
    """The full preset object for a set of CARP days."""
    ex = Expander(net)
    out_days, over, landings = [], [], set()
    for i, d in enumerate(days, 1):
        arcs = ex.day(d['route'])
        secs = sum(a['seconds'] for a in arcs)
        start, end = arcs[0]['from'], arcs[-1]['to']
        for n in (start, end):
            if n in FERRY_NAMES:
                landings.add(n)
        out_days.append({'day': i, 'start_node': start, 'end_node': end,
                         'arcs': arcs})
        if secs > budget:
            over.append({'day': i, 'seconds': secs, 'over_by': secs - budget})
    if total_required_miles is None:
        total_required_miles = sum(
            float(ex.by_id[e]['Miles']) for e in net.required)
    return {
        'circuit': 'Open',
        'hiking_style': 'supported',
        'n_days': len(out_days),
        'total_required_miles': total_required_miles,
        'start_node': out_days[0]['start_node'] if out_days else None,
        'days': out_days,
        'days_over_budget': over,
        'ferry_landings': [{'node': n, 'name': FERRY_NAMES[n]}
                           for n in sorted(landings)],
    }


def check(net, preset):
    """Assert what the validator will assert, before writing anything out."""
    seen = {}
    for day in preset['days']:
        for a in day['arcs']:
            if not a['is_deadhead']:
                seen[a['edge_id']] = seen.get(a['edge_id'], 0) + 1
        for u, v in zip(day['arcs'], day['arcs'][1:]):
            assert u['to'] == v['from'], f"day {day['day']} is not contiguous"
        assert day['arcs'][0]['from'] in net.access, \
            f"day {day['day']} starts at {day['arcs'][0]['from']}, no pick-up"
        assert day['arcs'][-1]['to'] in net.access, \
            f"day {day['day']} ends at {day['arcs'][-1]['to']}, no pick-up"
    missing = set(net.required) - set(seen)
    twice = {e for e, n in seen.items() if n > 1}
    assert not missing, f"{len(missing)} required edges uncovered"
    assert not twice, f"{len(twice)} required edges covered twice"
    return len(seen)
