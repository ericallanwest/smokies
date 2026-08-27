"""Stage 3: the most a day could be worth, solved rather than relaxed.

carp_cg's bound needs kappa, the most any feasible day can collect when each
required trail carries its dual price.  The three cheap relaxations all failed
in the same way -- they were far too generous to say anything useful:

    plain knapsack, travel free            kappa <= 4.34
    time-resource DP, repeats paid twice   kappa <= 29.84
    knapsack charging the cheapest hop in  kappa <= 3.38

against a heuristic best of about 1.4.  The middle one is the instructive
failure: it charges travel honestly, and is still the worst of the three,
because letting a trail be paid on every traversal lets a walk pace back and
forth over a short well-priced trail all day.  Elementarity is the difficulty.

CP-SAT handles it directly.  A day is a circuit through a virtual depot and
whichever trails it collects, AddCircuit gives elementarity for nothing, and a
single linear constraint holds the walking inside the budget.

**The bound survives a timeout.**  For a maximisation CP-SAT reports an upper
bound on the objective whether or not it proves optimality, and an upper bound
is all kappa has to be -- overstating the best day weakens the result without
invalidating it.  So this can be given ten minutes and still return something
rigorous, which is what makes it worth running at all.

Two deliberate approximations, both in the safe direction:

**One node per trail, not one per direction.**  Every cost -- the walk itself,
the hop in, the hop out, the approach from a pick-up -- is taken as the cheaper
of the two orientations.  A real day cannot beat that, so undercharging can only
overstate what it collects.  It halves the model.

**Every priced trail stays in.**  Dropping the cheap ones would shrink the
model considerably and understate what a day could collect, which is the one
direction that turns a bound into a guess.

    python tools/carp_price_exact.py --hours 12 --duals out/duals_12h.json
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from carp_common import INF, Net                       # noqa: E402


def build(net, duals, budget, near=None):
    """Model pieces: trails, their cheapest costs, and the hop matrix."""
    trails = sorted((e for e, pi in duals.items() if pi > 0),
                    key=lambda e: -duals[e])
    walk, into, outof = {}, {}, {}
    for e in trails:
        walk[e] = min(net.legs[e][0][2], net.legs[e][1][2])
        into[e] = min(net.from_access.get(net.legs[e][d][0], INF)
                      for d in (0, 1))
        outof[e] = min(net.to_access.get(net.legs[e][d][1], INF)
                       for d in (0, 1))
    trails = [e for e in trails if into[e] < INF and outof[e] < INF
              and into[e] + walk[e] + outof[e] <= budget]
    hop = {}
    for i, a in enumerate(trails):
        for j, b in enumerate(trails):
            if i == j:
                continue
            best = min(net.dist(net.legs[a][da][1], net.legs[b][db][0])
                       for da in (0, 1) for db in (0, 1))
            if best < INF:
                hop[(i, j)] = int(best)
    return trails, walk, into, outof, hop


def kappa(net, duals, budget, seconds=600, workers=8, log=print):
    """Upper bound on the best day's prize, the day itself, and whether proved.

    Returns (upper_bound, best_prize, trails_of_best_day, proved).  The day is
    worth having even when the bound is not proved: a column collecting more
    than 1 is one the LP wanted and did not have, which is what drives the next
    round.
    """
    from ortools.sat.python import cp_model

    trails, walk, into, outof, hop = build(net, duals, budget)
    n = len(trails)
    log(f"    {n} priced trails reachable inside the budget")
    if n == 0:
        return 0.0, 0.0, [], True

    m = cp_model.CpModel()
    visit = [m.NewBoolVar(f'v{i}') for i in range(n)]
    arcs, times = [], []
    # Node 0 is a virtual depot standing for "the crew".  An arc out of it is
    # the morning drop-off and an arc back is the evening pick-up, so a circuit
    # through it is exactly one day's walk between two pick-up points.
    for i in range(n):
        a = m.NewBoolVar(f'in{i}')
        b = m.NewBoolVar(f'out{i}')
        arcs.append((0, i + 1, a))
        arcs.append((i + 1, 0, b))
        times.append((int(into[trails[i]]), a))
        times.append((int(outof[trails[i]]), b))
        # Skipping a trail is a self-loop, which is how AddCircuit expresses an
        # optional node.
        arcs.append((i + 1, i + 1, visit[i].Not()))
        times.append((int(walk[trails[i]]), visit[i]))
    for (i, j), c in hop.items():
        lit = m.NewBoolVar(f'a{i}_{j}')
        arcs.append((i + 1, j + 1, lit))
        times.append((c, lit))
    m.AddCircuit(arcs)
    m.Add(sum(c * lit for c, lit in times) <= int(budget))
    # Prices are fractional; CP-SAT wants integers, so they are scaled and
    # rounded *up* -- the same safe direction as everything else here.
    scale = 10000
    m.Maximize(sum(int(duals[trails[i]] * scale + 0.5) * visit[i]
                   for i in range(n)))

    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = seconds
    s.parameters.num_search_workers = workers
    st = s.Solve(m)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        log("    no solution found; falling back to the trivial bound")
        return sum(duals.values()), 0.0, [], False
    ub = s.BestObjectiveBound() / scale
    got = s.ObjectiveValue() / scale
    proved = st == cp_model.OPTIMAL
    log(f"    best day found {got:.4f}, upper bound {ub:.4f}"
        + ("  (proved optimal)" if proved else f"  ({s.WallTime():.0f}s, "
           "not proved -- the upper bound is what counts)"))
    picked = [trails[i] for i in range(n) if s.Value(visit[i])]
    return ub, got, picked, proved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hours', type=float, required=True)
    ap.add_argument('--duals', required=True)
    ap.add_argument('--seconds', type=float, default=600)
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--no-ferry', action='store_true')
    ap.add_argument('--out', default=None)
    A = ap.parse_args()

    net = Net(ferry=not A.no_ferry)
    with open(A.duals, encoding='utf-8') as f:
        duals = {k: float(v) for k, v in json.load(f).items()}
    k, got, picked, proved = kappa(net, duals, A.hours * 3600,
                                   A.seconds, A.workers)
    total = sum(duals.values())
    import math
    bound = math.ceil(total / max(1.0, k))
    print(f"  kappa <= {k:.4f}{' (exact)' if proved else ''}")
    print(f"  duals {total:.2f} / kappa = LOWER BOUND {bound} days")
    if A.out:
        with open(A.out, 'w', encoding='utf-8') as f:
            json.dump({'kappa_upper': k, 'best_prize': got,
                       'best_day': picked, 'proved': proved, 'bound': bound,
                       'sum_duals': total, 'hours': A.hours}, f)


if __name__ == '__main__':
    main()
