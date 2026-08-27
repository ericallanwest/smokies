"""Stage 2: the set-covering LP, and a lower bound that holds whatever it says.

Six cycles of construct-bank-cover took 12 h from 42 days to 36 and 8 h from
impossible to 58, and nothing in that loop can say whether 36 is nearly optimal
or still far off.  This can, within a gap it also reports.

The model treats one feasible day as a column:

    minimise    sum over columns of x_r                       (days)
    subject to  sum of x_r over columns covering trail e >= 1  for every trail
                x_r >= 0

Covering rather than partitioning, because a trail walked twice is physically
fine -- it is what the map's "repeat" label already shows -- and the covering
LP is never the weaker bound.

**Why the LP value alone is not a bound.**  Solved over a subset of columns it
comes back *above* the true LP, and the true LP is what sits below the integer
optimum.  A restricted LP is therefore an upper bound on a lower bound, which
is worth nothing.  What converts it is Farley's ratio:

    L  >=  ceil( sum of duals / max(1, kappa) )
    kappa = the most any single feasible day can collect at those duals

and two properties of that make this affordable.  An *upper* bound on kappa is
enough -- overstating what the best day could collect only weakens the result,
never invalidates it -- so a pricer that allows days a hiker could not walk is
safe.  And any duals work, so stopping early gives a worse bound rather than a
wrong one.  The ceiling is legitimate because days are counted in whole numbers.

This module ships the LP and a heuristic pricer.  The heuristic finds columns
the LP wants, which sharpens the duals; it cannot certify kappa, so the bound
printed here uses whatever kappa the exact pricer in carp_price.py supplies,
and says so plainly when it is running without one.

    python tools/carp_cg.py --hours 12 --bank out/bank.json
"""
import argparse
import glob
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import carp_pool                                       # noqa: E402
import carp_price_exact                                # noqa: E402
import carp_preset                                     # noqa: E402
from carp_common import Net, day_optimum               # noqa: E402
from carp_search import insertion as _insert_delta      # noqa: E402


def solve_lp(n_edges, cols, by_edge):
    """The covering LP.  Returns (objective, duals) or None.

    GLOP rather than CP-SAT: this is a linear relaxation, the duals are the
    whole point, and CP-SAT does not expose them.
    """
    from ortools.linear_solver import pywraplp

    s = pywraplp.Solver.CreateSolver('GLOP')
    if s is None:
        return None
    x = [s.NumVar(0, 1, f'x{i}') for i in range(len(cols))]
    cons = {}
    for e, idx in by_edge.items():
        c = s.Constraint(1, s.infinity())
        for i in idx:
            c.SetCoefficient(x[i], 1)
        cons[e] = c
    s.Minimize(s.Sum(x))
    if s.Solve() != pywraplp.Solver.OPTIMAL:
        return None
    return s.Objective().Value(), {e: c.DualValue() for e, c in cons.items()}


def price_heuristic(net, cols, duals, budget, rounds=400):
    """Days that collect more than 1 at these prices, found greedily.

    Not a certificate -- it can miss a column that exists.  Its job is to feed
    the LP better columns so the duals stop moving, which is what makes the
    Farley ratio worth computing at all.
    """
    found, best = [], 0.0
    order = sorted(duals, key=lambda e: -duals[e])
    for start in order[:rounds]:
        if duals[start] <= 0:
            break
        route, cost = None, 0
        for d in (0, 1):
            c = net.enter((start, d)) + net.leave((start, d))
            if c <= budget and (route is None or c < cost):
                route, cost = [(start, d)], c
        if route is None:
            continue
        prize = duals[start]
        improved = True
        while improved:
            improved = False
            cand = None
            for e, pi in duals.items():
                if pi <= 0 or any(e == k for k, _ in route):
                    continue
                for d in (0, 1):
                    for pos in range(len(route) + 1):
                        delta = _insert_delta(net, route, (e, d), pos)
                        if delta >= float('inf') or cost + delta > budget:
                            continue
                        # Rank by price per hour: a trail worth 0.4 that costs
                        # ten minutes beats one worth 0.5 that costs an hour.
                        score = pi / max(60.0, delta)
                        if cand is None or score > cand[0]:
                            cand = (score, delta, e, d, pos)
            if cand:
                _, delta, e, d, pos = cand
                route.insert(pos, (e, d))
                cost += delta
                prize += duals[e]
                improved = True
        best = max(best, prize)
        if prize > 1.0 + 1e-9:
            found.append((route, cost, prize))
    return found, best





def kappa_knapsack(net, duals, budget, bucket=60):
    """An upper bound on what any feasible day could collect at these prices.

    Relaxed by dropping everything that connects the trails: a day is treated as
    a bag of required trails whose walking time fits the budget, with the
    approach, the egress and every step between them free.  No real day is
    cheaper than that, so no real day collects more than this -- which is the
    direction that keeps the bound valid.  It is loose in exactly the way the
    park is not, since a supported day spends most of its hours travelling, and
    tightening it is what the exact pricer in carp_price.py is for.

    A 0/1 knapsack over 415 trails and a budget in minute buckets, which is
    small enough to solve exactly.
    """
    items = [(min(net.legs[e][0][2], net.legs[e][1][2]) // bucket, duals[e])
             for e in net.required if duals[e] > 0]
    cap = int(budget // bucket)
    dp = [0.0] * (cap + 1)
    for w, v in items:
        w = max(1, int(w))
        if w > cap:
            continue
        for c in range(cap, w - 1, -1):
            alt = dp[c - w] + v
            if alt > dp[c]:
                dp[c] = alt
    return max(dp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hours', default='12')
    ap.add_argument('--bank', default=os.path.join('out', 'bank.json'))
    ap.add_argument('--data', default=os.path.join('docs', 'data'))
    ap.add_argument('--iterations', type=int, default=15)
    ap.add_argument('--exact', type=float, default=0,
                    help='seconds per round of exact CP-SAT pricing; with this '
                         'the loop can end in a proof rather than a guess')
    ap.add_argument('--dump-columns', default=None,
                    help='append every day the pricer invented to this bank, '
                         'so carp_pool can build an itinerary out of them')
    ap.add_argument('--dump-duals', default=None,
                    help='write the final duals here, for tools/carp_price.py')
    ap.add_argument('--kappa', type=float, default=None,
                    help='an upper bound on the best day at the final duals, '
                         'from tools/carp_price.py; without it no valid bound '
                         'is printed')
    A = ap.parse_args()

    dirs = [A.data] + sorted(glob.glob(os.path.join('out', 'carp*')))
    dirs = [d for d in dirs if os.path.isdir(d)]
    nets = {True: Net(ferry=True), False: Net(ferry=False)}
    exs = {k: carp_preset.Expander(v) for k, v in nets.items()}
    routes = carp_pool.harvest(dirs, nets[True])
    seen = {frozenset(e for e, _ in r) for r in routes}
    if os.path.exists(A.bank):
        with open(A.bank, encoding='utf-8') as f:
            for r in json.load(f)['routes']:
                route = [(e, d) for e, d in r]
                k = frozenset(e for e, _ in route)
                if k not in seen:
                    seen.add(k)
                    routes.append(route)
    print(f"{len(routes)} candidate days")

    for h in [int(x) for x in A.hours.split(',')]:
        budget = h * 3600
        pre_path = os.path.join(A.data, f'preset_supported_{h}h.json')
        pre = json.load(open(pre_path, encoding='utf-8'))
        net = nets[bool(pre.get('ferry_landings'))]
        ex = exs[bool(pre.get('ferry_landings'))]
        priced = carp_pool.price_all(net, routes, ex)
        cols = [c for c in priced if c['seconds'] <= budget]
        # Some tiers have trails no day can cover inside the budget -- at 8 h
        # there are four, and they are exactly the ones the published itinerary
        # declares as running over.  Held to the strict budget the LP is simply
        # infeasible, so those tiers are bounded against the same relaxed
        # problem the itinerary solves: days may run a quarter over.
        strict = {e for c in cols for e in c['covers']}
        relaxed = len(strict) < len(net.required)
        if relaxed:
            cols = [c for c in priced
                    if c['seconds'] <= budget * carp_pool.HARD_MULTIPLE]
        print(f"{os.linesep}=== {h} h ===  {len(cols)} columns, "
              f"published {pre['n_days']} days"
              + ("  (days up to a quarter over allowed, as the itinerary does)"
                 if relaxed else ""))

        # Never carried over from the previous tier: when a tier breaks out of
        # the loop early, reporting the last tier's duals silently prints one
        # tier's answer under another's heading.
        duals, obj, best_prize = None, None, None
        for it in range(1, A.iterations + 1):
            by_edge = {e: [] for e in net.required}
            for i, c in enumerate(cols):
                for e in c['covers']:
                    by_edge[e].append(i)
            if any(not v for v in by_edge.values()):
                miss = sum(1 for v in by_edge.values() if not v)
                print(f"  {miss} trails no column covers; LP infeasible")
                break
            got = solve_lp(len(net.required), cols, by_edge)
            if got is None:
                print("  LP did not solve")
                break
            obj, duals = got
            new, best_prize = price_heuristic(net, cols, duals, budget)
            exact_ub, proved = None, False
            if A.exact and not new:
                # The heuristic giving up is not the same as there being
                # nothing left, and at 12 h it was demonstrably not: it topped
                # out at 1.05 where CP-SAT found a day worth 1.56.  So the
                # exact pricer runs precisely when the cheap one is finished,
                # and its answer decides whether this is convergence or only
                # exhaustion.
                exact_ub, got_p, picked, proved = carp_price_exact.kappa(
                    net, duals, budget, A.exact, log=lambda *_: None)
                if got_p > 1.0 + 1e-9 and picked:
                    # The pricing model undercharges on purpose -- every cost is
                    # the cheaper of the two orientations -- so the day it names
                    # can be dearer than the budget once routed for real.  That
                    # is harmless for the bound and fatal for the column, so the
                    # cheapest trails come off until it fits.  What is left is
                    # worth less than the pricer claimed but is a genuine day,
                    # and if it still beats 1 the LP wanted it.
                    keep = sorted(picked, key=lambda e: -duals[e])
                    while keep:
                        cost, route = day_optimum(net, keep, 13)
                        if route and cost <= budget:
                            break
                        keep.pop()
                    prize = sum(duals[e] for e in keep)
                    if keep and prize > 1.0 + 1e-9:
                        new = [(route, cost, prize)]
                    best_prize = max(got_p, prize if keep else 0.0)
            print(f"  iter {it:>2}  LP {obj:7.2f}   best day collects "
                  f"{best_prize:5.2f}   {len(new)} new columns"
                  + (f"   [exact: <= {exact_ub:.3f}"
                     + (", proved]" if proved else "]") if exact_ub else ""))
            if not new:
                break
            for route, cost, _ in new:
                covers = {e for e, _ in route}
                cols.append({'route': route, 'legs': covers,
                             'seconds': cost, 'covers': covers})

        if A.dump_duals:
            dest = A.dump_duals.replace('TIER', str(h))
            os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
            with open(dest, 'w', encoding='utf-8') as f:
                json.dump({k: v for k, v in duals.items() if v > 0}, f)
            print(f"  duals -> {dest} "
                  f"({sum(1 for v in duals.values() if v > 0)} priced trails)")
        if A.dump_columns:
            # The pricer invents days to satisfy the LP, and they are days no
            # construction happened to build -- which is exactly what the pool
            # cover wants.  Whether they make a better itinerary is a question
            # for carp_pool, not for the LP.
            bank = {}
            if os.path.exists(A.dump_columns):
                with open(A.dump_columns, encoding='utf-8') as f:
                    for r in json.load(f)['routes']:
                        bank[frozenset(e for e, _ in r)] = [(e, d) for e, d in r]
            before = len(bank)
            for c in cols:
                bank.setdefault(frozenset(e for e, _ in c['route']), c['route'])
            with open(A.dump_columns, 'w', encoding='utf-8') as f:
                json.dump({'routes': [list(map(list, r))
                                      for r in bank.values()]}, f)
            print(f"  columns -> {A.dump_columns} "
                  f"({len(bank) - before} new, {len(bank)} total)")
        if duals is None:
            print("  no LP solved for this tier; nothing to report")
            continue
        total_dual = sum(duals.values())
        kappa = A.kappa or kappa_knapsack(net, duals, budget)
        bound = math.ceil(total_dual / max(1.0, kappa))
        how = 'exact pricer' if A.kappa else 'knapsack relaxation'
        # Two numbers, and it matters which is which.
        #
        # The LP value is the informative one: at convergence -- once no day
        # can collect more than 1 -- the restricted LP equals the true LP and
        # is a valid lower bound on the day count.  Short of convergence it is
        # an estimate sitting *above* the true LP, so it is reported as an
        # estimate and not as a bound.
        #
        # Farley's ratio is valid at any point, but only as strong as the kappa
        # behind it, and every relaxation tried so far is far too loose to be
        # worth acting on.  It is printed for completeness rather than use.
        # Convergence here is the *heuristic* pricer giving up, which is not a
        # certificate: it can miss a column that exists, and any column it
        # missed would have pushed the LP lower.  So even a converged figure is
        # an estimate of the true LP rather than a proof of it, and the true LP
        # is what would bound the day count.  Only an exact pricer closes that,
        # which is why the phrasing below never promises "at least".
        conv = best_prize <= 1.0 + 1e-9
        print(f"  LP {obj:.2f}  -- "
              + ("the heuristic pricer found nothing better; likely close to "
                 f"the true LP, so roughly {math.ceil(obj)} days"
                 if conv else
                 f"still descending (a day collects {best_prize:.2f}), so "
                 f"{math.ceil(obj)} days is an optimistic estimate")
              + ", not a proven bound")
        print(f"  published {pre['n_days']} days, "
              f"{pre['n_days'] - math.ceil(obj)} above that figure")
        print("  (a restricted LP sits above the true one, so this is where "
              "the optimum plausibly is, not a floor under it)")
        print(f"  [Farley: duals {total_dual:.2f} / kappa <= {kappa:.2f} "
              f"({how}) = {bound} days, valid but far too loose to use]")


if __name__ == '__main__':
    main()
