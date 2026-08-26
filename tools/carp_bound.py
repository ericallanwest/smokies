"""How far from optimal could the supported itineraries be?

CARP is NP-hard and the search that produces these presets is a heuristic, so
it cannot certify its own answer.  What it can do is be measured against a
lower bound computed a different way: if no itinerary can use fewer than L days
and ours uses D, the gap is at most D - L, whatever the true optimum is.

Two bounds, both rigorous, neither expensive.

**Capacity.**  Every required trail has to be walked at least once, and the
cheapest way to walk it is in its downhill direction.  Sum that over all 415
and divide by the daily budget.  It assumes approach and egress are free, which
they are emphatically not, so it is weak -- but it holds for any itinerary.

**Conflict.**  Two required trails cannot share a day if the cheapest possible
day covering both already exceeds the budget.  Compute that for every pair, and
any set of trails that mutually conflict needs one day each.  Finding the
largest such set is itself NP-hard, so a greedy clique is used: it may be
smaller than the best one, which only makes the bound safer.

The conflict bound is the one that says something about *this* park.  It is
driven by the remote high country -- trails that are hours from a road at both
ends, where a day can hold one of them and nothing else -- so it tightens as
the budget shrinks, exactly where the capacity bound is least informative.

    python tools/carp_bound.py
    python tools/carp_bound.py --hours 8,12 --data docs/data
"""
import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from carp_common import INF, Net                       # noqa: E402


def pair_cost(net, e, f):
    """Cheapest pick-up-to-pick-up day covering both trails, either order."""
    best = INF
    for a, b in ((e, f), (f, e)):
        for da in (0, 1):
            ta, ha, wa = net.legs[a][da]
            head_in = net.from_access.get(ta, INF)
            if head_in >= INF:
                continue
            for db in (0, 1):
                tb, hb, wb = net.legs[b][db]
                mid = net.dist(ha, tb)
                out = net.to_access.get(hb, INF)
                if mid >= INF or out >= INF:
                    continue
                c = head_in + wa + mid + wb + out
                if c < best:
                    best = c
    return best


def conflict_bound(net, budget, log=print):
    """Greedy clique of trails that pairwise cannot share a day."""
    req = list(net.required)
    # Only trails whose own cheapest day is a large fraction of the budget can
    # conflict with much, so this prunes the pair scan hard without weakening
    # the bound: anything excluded here could not have joined the clique.
    solo = {e: net.solo(e) for e in req}
    cand = sorted((e for e in req if solo[e] > budget / 2),
                  key=lambda e: -solo[e])
    log(f"    {len(cand)} of {len(req)} trails could conflict with another")
    if not cand:
        return 0, []
    conflicts = {e: set() for e in cand}
    for i, e in enumerate(cand):
        for f in cand[i + 1:]:
            if pair_cost(net, e, f) > budget:
                conflicts[e].add(f)
                conflicts[f].add(e)
    # Greedy: take the most-conflicted trail, then whatever still conflicts
    # with everything already taken.
    clique = []
    pool = sorted(cand, key=lambda e: (-len(conflicts[e]), -solo[e]))
    for e in pool:
        if all(f in conflicts[e] for f in clique):
            clique.append(e)
    return len(clique), clique


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hours', default='8,9,10,11,12,13,14,15,16')
    ap.add_argument('--data', default=os.path.join('docs', 'data'))
    A = ap.parse_args()

    nets = {True: Net(ferry=True), False: Net(ferry=False)}
    print(f"{'tier':>5}{'capacity':>10}{'conflict':>10}{'bound':>8}"
          f"{'ours':>7}{'gap':>7}  worst pair-conflict trail")
    for h in [int(x) for x in A.hours.split(',')]:
        fp = os.path.join(A.data, f'preset_supported_{h}h.json')
        if not os.path.exists(fp):
            continue
        pre = json.load(open(fp, encoding='utf-8'))
        net = nets[bool(pre.get('ferry_landings'))]
        budget = h * 3600
        cheapest = sum(min(net.legs[e][0][2], net.legs[e][1][2])
                       for e in net.required)
        cap = math.ceil(cheapest / budget)
        n, clique = conflict_bound(net, budget, log=lambda *_: None)
        bound = max(cap, n)
        ours = pre['n_days']
        name = ''
        if clique:
            worst = max(clique, key=net.solo)
            row = next(r for r in net.rows
                       if str(float(r['ID'])) == worst)
            name = f"{row['Trail']} ({net.solo(worst) / 3600:.1f} h)"
        print(f"{h:>4}h{cap:>10}{n:>10}{bound:>8}{ours:>7}"
              f"{100 * (ours - bound) / ours:>6.0f}%  {name}")
    print()
    print(f"required walking, cheapest direction: {cheapest / 3600:.1f} h")
    print("The capacity bound assumes approach and egress are free; the "
          "conflict bound counts only days forced by a single remote trail. "
          "The true optimum is somewhere above both.")


if __name__ == '__main__':
    main()
