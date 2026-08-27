"""Stage 3: what is the most a single day could be worth at these prices?

The bound in carp_cg needs kappa -- the most any feasible day can collect when
each required trail carries its dual price.  An *upper* bound is enough, since
overstating the best day only weakens the result, and that is what makes this
tractable: the relaxation can allow days a hiker would not walk, so long as it
never allows one that is cheaper than reality.

The knapsack relaxation carp_cg falls back on treats a day as a bag of trails
whose walking time fits the budget, with every step between them free.  In a
park where a supported day spends most of its hours simply getting to the trail
that is hopeless -- it returned kappa <= 5.42 at 12 h against a heuristic best
of 1.59, and a bound of six days against a published 36.

This charges the travel.  A day is a walk through the graph from one pick-up to
another, time accumulates on every arc, and a trail pays its dual when walked.
Because every arc costs strictly positive time, states ordered by elapsed time
form a DAG and the whole thing is one forward pass:

    f[node][t] = the most collectable arriving at node having spent t

with the answer read off the pick-up points.  Time is bucketed to the minute and
arc costs are rounded *down*, so the relaxation stays on the safe side: paths
look slightly cheaper than they are, never dearer.

What is still relaxed is elementarity -- a trail walked twice is paid twice --
so kappa comes back above the truth.  Tightening that is decremental state space
relaxation: solve, look at what the winning walk repeats, and re-solve tracking
only those few trails.  --dssr runs that loop.

    python tools/carp_price.py --hours 12 --duals out/duals_12h.json
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from carp_common import Net                            # noqa: E402

BUCKET = 60


def build_arcs(net, duals):
    """Every arc as (u, v, buckets, prize), prize only on priced trails.

    A required trail can appear as several arcs if the graph holds parallel
    edges, so the price goes on the specific traversal, keyed by node pair and
    the edge it belongs to.
    """
    priced = {}
    for eid, pi in duals.items():
        if pi <= 0:
            continue
        for d in (0, 1):
            t, h, _ = net.legs[eid][d]
            priced.setdefault((t, h), []).append((eid, pi))
    arcs = {}
    for u, v, data in net.G.edges(data=True):
        w = max(1, int(data['weight']) // BUCKET)
        best = max((p for _, p in priced.get((u, v), [])), default=0.0)
        eid = next((e for e, p in priced.get((u, v), []) if p == best), None)
        arcs.setdefault(u, []).append((v, w, best, eid))
    return arcs


def best_day(net, duals, budget, forbid_repeat=(), trace=False):
    """Upper bound on the prize of one pick-up-to-pick-up day.

    forbid_repeat names trails that may be paid at most once; everything else
    is paid on every traversal.  With it empty this is the plain relaxation.
    """
    arcs = build_arcs(net, duals)
    cap = int(budget // BUCKET)
    keys = list(forbid_repeat)
    idx = {e: i for i, e in enumerate(keys)}
    masks = 1 << len(keys)
    NEG = -1.0
    # f[t][node][mask]; stored sparsely because most states are unreachable.
    f = [dict() for _ in range(cap + 1)]
    back = [dict() for _ in range(cap + 1)] if trace else None
    for n in net.access:
        f[0][(n, 0)] = 0.0
    best, best_state = NEG, None
    for t in range(cap + 1):
        layer = f[t]
        if not layer:
            continue
        for (node, mask), val in layer.items():
            if node in net.access and val > best:
                best, best_state = val, (t, node, mask)
            for v, w, prize, eid in arcs.get(node, ()):
                nt = t + w
                if nt > cap:
                    continue
                nm, gain = mask, prize
                if eid is not None and eid in idx:
                    bit = 1 << idx[eid]
                    if mask & bit:
                        gain = 0.0
                    else:
                        nm = mask | bit
                k = (v, nm)
                nv = val + gain
                if nv > f[nt].get(k, NEG):
                    f[nt][k] = nv
                    if trace:
                        back[nt][k] = (t, node, mask, eid)
    if not trace:
        return best, None
    # Walk the winner back to see which trails it paid for more than once.
    seen, repeats, cur = {}, set(), best_state
    while cur is not None:
        t, node, mask = cur
        prev = back[t].get((node, mask))
        if prev is None:
            break
        pt, pnode, pmask, eid = prev
        if eid is not None:
            seen[eid] = seen.get(eid, 0) + 1
            if seen[eid] > 1:
                repeats.add(eid)
        cur = (pt, pnode, pmask)
    return best, repeats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hours', type=float, required=True)
    ap.add_argument('--duals', required=True,
                    help='JSON mapping edge id -> dual, from carp_cg --dump-duals')
    ap.add_argument('--no-ferry', action='store_true')
    ap.add_argument('--dssr', type=int, default=0,
                    help='rounds of decremental state space relaxation')
    A = ap.parse_args()

    net = Net(ferry=not A.no_ferry)
    with open(A.duals, encoding='utf-8') as f:
        duals = {k: float(v) for k, v in json.load(f).items()}
    budget = A.hours * 3600
    forbid = set()
    for rnd in range(A.dssr + 1):
        kappa, repeats = best_day(net, duals, budget, forbid,
                                  trace=bool(A.dssr))
        print(f"  round {rnd}: kappa <= {kappa:.4f}"
              + (f"  ({len(forbid)} trails held to one payment)" if forbid else ""))
        if not repeats:
            break
        forbid |= set(list(repeats)[:12 - len(forbid)])
        if len(forbid) >= 12:
            print("  memory full; stopping while the bound is still valid")
            break
    print(json.dumps({'kappa_upper': kappa, 'hours': A.hours,
                      'held': sorted(forbid)}))


if __name__ == '__main__':
    main()
