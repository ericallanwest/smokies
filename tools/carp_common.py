"""Graph, distances and exact per-day routing, shared by the CARP tools.

Split out of tools/day_route_optimum.py so the clustering in
tools/carp_supported.py routes days the same way the falsification test did.

One correction on the way out.  day_route_optimum named its two multi-source
tables the wrong way round and so charged each day the cost of walking *out* to
the road in the morning and *in* from it at night.  On a graph where uphill and
downhill cost differently that is not the same number.  The names here say which
direction they mean and are checked against the solver's own usage
(smokies_circuit_solver_20260509a.py:1759).
"""
import csv
import os

import networkx as nx

FERRY = {'TI051', 'TI053', 'TI064', 'BC090'}
CSV_NAME = 'smokies_edge_list_20260509a.csv'
INF = float('inf')


def find_csv(start='.'):
    for d in (start, 'solver', os.path.join('..', 'solver'), '..'):
        p = os.path.join(d, CSV_NAME)
        if os.path.exists(p):
            return p
    raise SystemExit(f'cannot find {CSV_NAME}')


class Net:
    """The trail graph plus everything a day-cost needs."""

    def __init__(self, csv_path=None, ferry=True):
        rows = list(csv.DictReader(
            open(csv_path or find_csv(), encoding='utf-8-sig')))
        self.rows = [r for r in rows if not int(r.get('is_trail_closed') or 0)]
        G = nx.DiGraph()
        for r in self.rows:
            a, b = r['node_A'], r['node_B']
            for u, v, c in ((a, b, 'cost_A_to_B'), (b, a, 'cost_B_to_A')):
                w = int(r[c])
                if not G.has_edge(u, v) or G[u][v]['weight'] > w:
                    G.add_edge(u, v, weight=w)
        self.G = G
        self.access = {n for n in G if n[:2] in ('TH', 'CG')}
        if ferry:
            self.access |= (FERRY & set(G))
        # from_access[n]: nearest pick-up -> n, what the morning drop-off costs.
        # to_access[n]:   n -> nearest pick-up, what the evening walk-out costs.
        self.from_access = nx.multi_source_dijkstra_path_length(
            G, self.access, weight='weight')
        self.to_access = nx.multi_source_dijkstra_path_length(
            G.reverse(copy=False), self.access, weight='weight')
        self.D = dict(nx.all_pairs_dijkstra_path_length(G, weight='weight'))
        # Which pick-up each approach comes from, and by what path -- needed to
        # write an itinerary out rather than only to cost one.
        _, self.from_path = nx.multi_source_dijkstra(G, self.access,
                                                     weight='weight')
        _, back = nx.multi_source_dijkstra(G.reverse(copy=False), self.access,
                                           weight='weight')
        self.to_path = {n: list(reversed(p)) for n, p in back.items()}
        # The cheapest concrete arc behind each ordered node pair, so a
        # shortest path can be expanded back into trail names and mileages.
        self.arc = {}
        for r in self.rows:
            eid, a, b = str(float(r['ID'])), r['node_A'], r['node_B']
            for u, v, c, g, d in ((a, b, 'cost_A_to_B', 'gain_A_to_B', 'fwd'),
                                  (b, a, 'cost_B_to_A', 'gain_B_to_A', 'rev')):
                w = int(r[c])
                if (u, v) not in self.arc or self.arc[(u, v)]['seconds'] > w:
                    self.arc[(u, v)] = {
                        'edge_id': eid, 'trail': r['Trail'],
                        'miles': float(r['Miles']), 'seconds': w,
                        'gain': int(float(r[g] or 0)), 'direction': d}
        # Each required edge as its two orientations: (tail, head, seconds).
        self.legs = {}
        for r in self.rows:
            eid = str(float(r['ID']))
            a, b = r['node_A'], r['node_B']
            self.legs[eid] = ((a, b, int(r['cost_A_to_B'])),
                              (b, a, int(r['cost_B_to_A'])))
        # Non-required edges stay in the graph as connectors; only the
        # required ones have to be covered.
        self.required = [str(float(r['ID'])) for r in self.rows
                         if int(r.get('is_required') or 0)]

    def dist(self, u, v):
        return 0 if u == v else self.D.get(u, {}).get(v, INF)

    def leg(self, item):
        eid, d = item
        return self.legs[eid][d]

    def enter(self, item):
        """Pick-up to the start of this leg, plus the leg itself."""
        t, _, w = self.leg(item)
        return self.from_access.get(t, INF) + w

    def leave(self, item):
        """The end of this leg back to a pick-up."""
        _, h, _ = self.leg(item)
        return self.to_access.get(h, INF)

    def route_cost(self, route):
        """Total walking for an ordered list of (edge_id, direction)."""
        if not route:
            return 0
        c = self.from_access.get(self.leg(route[0])[0], INF)
        for k, item in enumerate(route):
            c += self.leg(item)[2]
            if k + 1 < len(route):
                c += self.dist(self.leg(item)[1], self.leg(route[k + 1])[0])
        return c + self.to_access.get(self.leg(route[-1])[1], INF)

    def solo(self, eid):
        """Cheapest day covering just this edge -- also its remoteness."""
        return min(self.enter((eid, d)) + self.leave((eid, d)) for d in (0, 1))


def day_optimum(net, edge_ids, exact_max=13):
    """Cheapest pick-up-to-pick-up walk covering exactly these required edges.

    A Rural Postman Problem, recast as a generalised ATSP path: each edge is a
    two-member cluster (its orientations) and Held-Karp runs over subsets.
    Above exact_max edges the state space stops being free, so it falls back to
    a multi-start nearest neighbour.  Returns (seconds, ordered route).
    """
    ids = list(edge_ids)
    n = len(ids)
    if n == 0:
        return 0, []
    items = [[(ids[i], 0), (ids[i], 1)] for i in range(n)]

    def hop(i, di, j, dj):
        return net.dist(net.leg(items[i][di])[1], net.leg(items[j][dj])[0])

    if n > exact_max:
        best, best_route = INF, None
        for s in range(n):
            for ds in (0, 1):
                cur, used = (s, ds), {s}
                route, cost = [items[s][ds]], net.enter(items[s][ds])
                while len(used) < n and cost < INF:
                    nxt = min(((hop(cur[0], cur[1], j, dj)
                                + net.leg(items[j][dj])[2], j, dj)
                               for j in range(n) if j not in used
                               for dj in (0, 1)), default=None)
                    if nxt is None or nxt[0] == INF:
                        cost = INF
                        break
                    cost += nxt[0]
                    cur = (nxt[1], nxt[2])
                    used.add(nxt[1])
                    route.append(items[cur[0]][cur[1]])
                if cost < INF:
                    total = cost + net.leave(route[-1])
                    if total < best:
                        best, best_route = total, list(route)
        return best, (best_route or [])

    full = (1 << n) - 1
    dp, par = {}, {}
    for i in range(n):
        for d in (0, 1):
            c = net.enter(items[i][d])
            if c < INF:
                dp[(1 << i, i, d)] = c
    for mask in range(1, full + 1):
        for i in range(n):
            if not mask & (1 << i):
                continue
            for d in (0, 1):
                cur = dp.get((mask, i, d))
                if cur is None:
                    continue
                for j in range(n):
                    if mask & (1 << j):
                        continue
                    for dj in (0, 1):
                        h = hop(i, d, j, dj)
                        if h == INF:
                            continue
                        k = (mask | (1 << j), j, dj)
                        v = cur + h + net.leg(items[j][dj])[2]
                        if v < dp.get(k, INF):
                            dp[k] = v
                            par[k] = (mask, i, d)
    best, end = INF, None
    for i in range(n):
        for d in (0, 1):
            c = dp.get((full, i, d))
            if c is None:
                continue
            t = c + net.leave(items[i][d])
            if t < best:
                best, end = t, (full, i, d)
    if end is None:
        return INF, []
    route = []
    while end is not None:
        route.append(items[end[1]][end[2]])
        end = par.get(end)
    route.reverse()
    return best, route
