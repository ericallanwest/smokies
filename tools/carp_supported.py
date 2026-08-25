"""CARP for supported itineraries, phases 1 and 2: seed the days, route each
day exactly.  No local search yet -- that is phase 3, and it is where the days
are expected to come from.

The published supported presets are built the wrong way round: one continuous
Eulerian walk over the whole park, cut into days afterwards.  The walk is never
told that a supported day has to begin and end where a vehicle can reach, so a
quarter to a third of all walking is approach and connector.

This inverts it.  Cover the 415 required edges with day-routes that each start
and end at a pick-up and fit the budget; minimise the number of days.

Phase 1 seeds geographically, from the hardest arc outwards.  Taking the edge
whose cheapest possible solo day is longest -- the remotest required trail --
and building a day around it is what gives the high country a day of its own
rather than whatever is left when the easy trails are gone.  Growth is by
cheapest insertion into the day's current route.

Phase 2 re-routes each finished day exactly (tools/carp_common.day_optimum).
Because the exact route is never worse than the insertion order that produced
it, finishing a day can free up time, so growth and exact routing alternate
until nothing more fits.

    python tools/carp_supported.py --hours 12
    python tools/carp_supported.py --hours 10,12,14 --out out/carp
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import carp_preset                                     # noqa: E402
import carp_search                                     # noqa: E402
from carp_common import INF, Net, day_optimum          # noqa: E402


def insertion(net, route, item, pos):
    """Cost of putting item at position pos in route, over the route's cost."""
    tail, head, w = net.leg(item)
    if not route:
        return net.from_access.get(tail, INF) + w + net.to_access.get(head, INF)
    if pos == 0:
        nxt = net.leg(route[0])[0]
        return (net.from_access.get(tail, INF) + w + net.dist(head, nxt)
                - net.from_access.get(nxt, INF))
    if pos == len(route):
        prv = net.leg(route[-1])[1]
        return (net.dist(prv, tail) + w + net.to_access.get(head, INF)
                - net.to_access.get(prv, INF))
    prv, nxt = net.leg(route[pos - 1])[1], net.leg(route[pos])[0]
    return net.dist(prv, tail) + w + net.dist(head, nxt) - net.dist(prv, nxt)


def near(net, cluster_nodes, remaining, k):
    """The k remaining edges closest to anything the day already touches."""
    scored = []
    for eid in remaining:
        best = INF
        for d in (0, 1):
            t = net.legs[eid][d][0]
            for n in cluster_nodes:
                v = net.dist(n, t)
                if v < best:
                    best = v
        if best < INF:
            scored.append((best, eid))
    scored.sort()
    return [eid for _, eid in scored[:k]]


def grow(net, route, cost, remaining, budget, k):
    """Insert edges one at a time while they fit.  Returns (route, cost)."""
    nodes = {n for it in route for n in net.leg(it)[:2]}
    while remaining:
        best = None
        for eid in near(net, nodes, remaining, k):
            for d in (0, 1):
                for pos in range(len(route) + 1):
                    delta = insertion(net, route, (eid, d), pos)
                    if delta < INF and (best is None or delta < best[0]):
                        best = (delta, eid, d, pos)
        if best is None or cost + best[0] > budget:
            return route, cost
        delta, eid, d, pos = best
        route.insert(pos, (eid, d))
        remaining.discard(eid)
        nodes.update(net.leg((eid, d))[:2])
        cost += delta
    return route, cost


def build(net, budget, exact_max, k, log=print):
    """Phase 1 + 2 over the whole required set.  Returns a list of days."""
    remaining = set(net.required)
    unreachable = {e for e in remaining if net.solo(e) == INF}
    if unreachable:
        log(f"  {len(unreachable)} required edges no pick-up can reach; skipped")
        remaining -= unreachable
    days = []
    while remaining:
        seed = max(remaining, key=lambda e: (net.solo(e), e))
        remaining.discard(seed)
        d = min((0, 1), key=lambda k2: net.enter((seed, k2)) + net.leave((seed, k2)))
        route, cost = [(seed, d)], net.solo(seed)
        if cost > budget:
            # No arrangement fits this edge in the budget.  It gets a day of
            # its own, declared over, rather than dragging a neighbour over
            # with it.
            days.append({'route': route, 'seconds': cost, 'over': True})
            continue
        while True:
            before = len(route)
            route, cost = grow(net, route, cost, remaining, budget, k)
            if len(route) <= exact_max:
                ecost, eroute = day_optimum(net, [e for e, _ in route], exact_max)
                if ecost < cost:
                    route, cost = eroute, ecost
            if len(route) == before:
                break
        days.append({'route': route, 'seconds': cost, 'over': cost > budget})
    return days


def report(net, days, budget, published=None):
    total = sum(d['seconds'] for d in days)
    over = [d for d in days if d['over']]
    covered = [e for d in days for e, _ in d['route']]
    assert len(covered) == len(set(covered)), 'an edge was assigned twice'
    print(f"  days            {len(days)}")
    print(f"  total walking   {total / 3600:.1f} h")
    print(f"  required cover  {len(covered)} of {len(net.required)} edges")
    if over:
        print(f"  over budget     {len(over)} days, longest "
              f"{max(d['seconds'] for d in over) / 3600:.2f} h")
    if published:
        pd, pw = published
        print(f"  published       {pd} days, {pw / 3600:.1f} h  ->  "
              f"{len(days) - pd:+d} days, {(total - pw) / 3600:+.1f} h")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hours', default='12',
                    help='comma-separated day budgets, e.g. 10,12,14')
    ap.add_argument('--exact-max', type=int, default=13,
                    help='route days up to this many edges exactly')
    ap.add_argument('--candidates', type=int, default=10,
                    help='nearest edges considered per insertion')
    ap.add_argument('--data', default=os.path.join('docs', 'data'))
    ap.add_argument('--out', default=None,
                    help='directory to write the day assignments into')
    ap.add_argument('--search', type=float, default=0,
                    help='seconds of phase-3 local search per tier')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--emit', default=None,
                    help='directory to write preset_supported_Nh.json into')
    ap.add_argument('--no-ferry', action='store_true')
    A = ap.parse_args()

    t0 = time.time()
    net = Net(ferry=not A.no_ferry)
    print(f"{len(net.required)} required edges, {net.G.number_of_nodes()} nodes, "
          f"{len(net.access)} pick-up points  ({time.time() - t0:.0f}s)")

    for h in [int(x) for x in A.hours.split(',')]:
        pub = None
        fp = os.path.join(A.data, f'preset_supported_{h}h.json')
        if os.path.exists(fp):
            p = json.load(open(fp, encoding='utf-8'))
            pub = (p['n_days'],
                   sum(a['seconds'] for day in p['days'] for a in day['arcs']))
        print(f"=== {h} h ===")
        t = time.time()
        days = build(net, h * 3600, A.exact_max, A.candidates)
        print(f"  built in        {time.time() - t:.0f}s")
        report(net, days, h * 3600, pub)
        if A.search:
            days = carp_search.search(net, days, h * 3600, A.exact_max,
                                      A.search, A.seed)
            print("  after search")
            report(net, days, h * 3600, pub)
        if A.out:
            os.makedirs(A.out, exist_ok=True)
            dest = os.path.join(A.out, f'carp_supported_{h}h.json')
            with open(dest, 'w', encoding='utf-8') as f:
                json.dump({'budget_hours': h, 'n_days': len(days),
                           'days': [{'seconds': d['seconds'], 'over': d['over'],
                                     'route': [[e, k] for e, k in d['route']]}
                                    for d in days]}, f, indent=1)
            print(f"  wrote           {dest}")
        if A.emit:
            os.makedirs(A.emit, exist_ok=True)
            pre = carp_preset.build_preset(net, days, h * 3600)
            n = carp_preset.check(net, pre)
            dest = os.path.join(A.emit, f'preset_supported_{h}h.json')
            with open(dest, 'w', encoding='utf-8') as f:
                json.dump(pre, f, indent=2)
            print(f"  emitted         {dest} ({n} required edges covered once)")


if __name__ == '__main__':
    main()
