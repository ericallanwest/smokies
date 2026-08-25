"""Step 1 of the CARP build order: can exact per-day routing beat the days we
already publish, on their own edge sets?

For each published day, take the required edges it covers and ask the cheapest
possible way to walk exactly those, starting and ending at any pick-up point.
That is a Rural Postman Problem, and with ~10 required edges it is small enough
to solve exactly: each edge becomes a cluster of two nodes (its two
orientations) and the whole day is a generalised ATSP path, solved by Held-Karp
over subsets.

If this cannot beat the current days, no amount of clustering or local search
built on top of it will, and CARP is not worth building.
"""
import csv, json, sys
from functools import lru_cache
import networkx as nx

EXACT_MAX = 13          # 2^13 * 26 * 26 states is a few seconds; 18 is not

rows = list(csv.DictReader(open('smokies_edge_list_20260509a.csv', encoding='utf-8-sig')))
G = nx.DiGraph()
for r in rows:
    if int(r.get('is_trail_closed') or 0):
        continue
    a, b = r['node_A'], r['node_B']
    for u, v, c in ((a, b, 'cost_A_to_B'), (b, a, 'cost_B_to_A')):
        w = int(r[c])
        if not G.has_edge(u, v) or G[u][v]['weight'] > w:
            G.add_edge(u, v, weight=w)

FERRY = {'TI051', 'TI053', 'TI064', 'BC090'}
access = {n for n in G if n[:2] in ('TH', 'CG')} | FERRY
out_of = nx.multi_source_dijkstra_path_length(G, access, weight='weight')
back_of = nx.multi_source_dijkstra_path_length(G.reverse(copy=False), access, weight='weight')
D = dict(nx.all_pairs_dijkstra_path_length(G, weight='weight'))

by_id = {}
for r in rows:
    if int(r.get('is_trail_closed') or 0):
        continue
    by_id[str(float(r['ID']))] = r

INF = float('inf')


def day_optimum(edge_ids):
    """Cheapest walk covering these required edges, pick-up to pick-up."""
    # Two orientations per edge; node k*2+d is edge k walked direction d.
    legs = []
    for eid in edge_ids:
        r = by_id[eid]
        a, b = r['node_A'], r['node_B']
        legs.append([(a, b, int(r['cost_A_to_B'])), (b, a, int(r['cost_B_to_A']))])
    n = len(legs)
    if n == 0:
        return 0

    def hop(i, di, j, dj):
        """Walk from the end of leg (i,di) to the start of leg (j,dj)."""
        return D.get(legs[i][di][1], {}).get(legs[j][dj][0], INF)

    def enter(i, di):
        return back_of.get(legs[i][di][0], INF) + legs[i][di][2]

    def leave(i, di):
        return out_of.get(legs[i][di][1], INF)

    if n > EXACT_MAX:                       # nearest-neighbour + 2-opt fallback
        best = INF
        for s in range(n):
            for ds in (0, 1):
                order, used, cur, cost = [(s, ds)], {s}, (s, ds), enter(s, ds)
                while len(used) < n:
                    nxt = min(((hop(*cur, j, dj) + legs[j][dj][2], j, dj)
                               for j in range(n) if j not in used
                               for dj in (0, 1)), default=None)
                    if nxt is None or nxt[0] == INF:
                        cost = INF
                        break
                    cost += nxt[0]
                    cur = (nxt[1], nxt[2])
                    used.add(nxt[1])
                    order.append(cur)
                if cost < INF:
                    best = min(best, cost + leave(*cur))
        return best

    FULL = (1 << n) - 1
    # dp[(mask, i, d)] = cheapest way to have covered mask, ending on leg (i,d)
    dp = {}
    for i in range(n):
        for d in (0, 1):
            c = enter(i, d)
            if c < INF:
                dp[(1 << i, i, d)] = min(dp.get((1 << i, i, d), INF), c)
    for mask in range(1, FULL + 1):
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
                        v = cur + h + legs[j][dj][2]
                        if v < dp.get(k, INF):
                            dp[k] = v
    best = INF
    for i in range(n):
        for d in (0, 1):
            c = dp.get((FULL, i, d))
            if c is not None:
                best = min(best, c + leave(i, d))
    return best


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'docs/data/preset_supported_12h.json'
    pre = json.load(open(path, encoding='utf-8'))
    print(f"{path}: {pre['n_days']} days\n")
    print(f"{'day':>4}{'edges':>7}{'now':>9}{'optimal':>10}{'saving':>9}  mode")
    tot_now = tot_opt = 0
    for day in pre['days']:
        eids = sorted({a['edge_id'] for a in day['arcs'] if not a['is_deadhead']})
        now = sum(a['seconds'] for a in day['arcs'])
        opt = day_optimum(eids)
        mode = 'exact' if len(eids) <= EXACT_MAX else 'heuristic'
        tot_now += now
        tot_opt += opt if opt < INF else now
        print(f"{day['day']:>4}{len(eids):>7}{now/3600:>8.2f}h{opt/3600:>9.2f}h"
              f"{(now-opt)/3600:>8.2f}h  {mode}")
    print(f"\ntotal now {tot_now/3600:.1f} h, optimal {tot_opt/3600:.1f} h, "
          f"saving {(tot_now-tot_opt)/3600:.1f} h "
          f"({100*(tot_now-tot_opt)/tot_now:.0f}%)")


main()
