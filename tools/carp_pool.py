"""Stage 1: build one itinerary out of every day we have ever generated.

Each run of the search produces forty-odd days and throws away the rest of what
it tried.  Across all the batches in out/ and everything published there are
several hundred distinct days sitting on disk, every one of them a real walk
from a pick-up to a pick-up inside somebody's budget.  Nothing has ever asked
whether a better itinerary can be assembled out of the union.

That question is a set-covering problem -- choose fewest days such that every
required trail is walked by at least one of them -- and CP-SAT answers it
exactly in seconds.  It is a far larger neighbourhood than any move the local
search makes: it can take day 12 from one run, day 31 from another and a day
built for a different budget entirely.

Two details make the pool bigger than it looks.

**Days cross tiers.**  A day built for a 10 h budget is legal at 12 h.  So each
tier draws on every run at every budget, keeping whatever fits, and the higher
tiers inherit hundreds of columns they never generated.

**A day covers more than it was credited with.**  What matters here is every
required trail the day *walks*, connectors included, since coverage can be
credited to any day that walks it.  That is the same observation
tools/carp_credit.py acts on, and it roughly doubles what each column offers.

Costs are recomputed against the tier being solved, because approach and egress
change with the pick-up set: the same route is cheaper with the ferry landings
available than without.

    python tools/carp_pool.py --hours 12
    python tools/carp_pool.py --emit out/pool --search 300
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import carp_credit                                     # noqa: E402
import carp_preset                                     # noqa: E402
import carp_search                                     # noqa: E402
from carp_common import INF, Net, day_optimum          # noqa: E402

HARD_MULTIPLE = 1.5      # the validator's ceiling for a declared long day


def harvest(dirs, net):
    """Every distinct day on disk, as an ordered list of required traversals.

    Deduplicated by the set of trails walked, so two runs finding the same day
    contribute one column.
    """
    seen, out = set(), []
    for d in dirs:
        for fp in sorted(glob.glob(os.path.join(d, 'preset_supported_*h.json'))):
            pre = json.load(open(fp, encoding='utf-8'))
            for w in carp_credit.days_from_preset(net, pre):
                if not w['route']:
                    continue
                key = frozenset(e for e, _ in w['route'])
                if key not in seen:
                    seen.add(key)
                    out.append(w['route'])
    return out


def columns_for(net, routes, budget, ceiling, ex):
    """Price each route against this tier and keep the ones that fit.

    Coverage is everything the day walks, not only its legs -- a connector that
    runs along a required trail covers it just as well.

    The route is rebuilt from the expanded walk rather than kept as it came in,
    so that what a column claims to cover and what it can actually be credited
    with are the same set.  They are not the same by default: expansion links
    the legs with shortest paths, and those paths run along required trails of
    their own.  Crediting one of those to this day would otherwise leave the
    trail with nowhere to live, which is what the coverage assertion in
    carp_credit.tighten catches.
    """
    cols = []
    required = set(net.required)
    for route in routes:
        cost = net.route_cost(route)
        if cost >= INF or cost > ceiling:
            continue
        arcs = ex.day(route)
        seen, full = set(), []
        for a in arcs:
            eid = a['edge_id']
            if eid in required and eid not in seen:
                d = carp_credit.direction(net, eid, a['from'], a['to'])
                if d is not None:
                    seen.add(eid)
                    full.append((eid, d))
        cols.append({'route': full, 'legs': {e for e, _ in route},
                     'seconds': sum(a['seconds'] for a in arcs),
                     'covers': seen, 'over': cost > budget})
    return cols


def solve_cover(net, cols, log=print, seconds=120):
    """Fewest days covering every trail, ranked the way the itinerary is.

    The objective has to be lexicographic and it has to lead with over-budget
    days.  Minimising the day count alone picks whichever days cover the most,
    which are precisely the ones that run longest -- at 8 h that returned 37
    days of which 34 were unwalkable, against a published 60 with 4.  Fewer
    days is not better if they are days nobody can walk.
    """
    from ortools.sat.python import cp_model

    by_edge = {e: [] for e in net.required}
    for i, c in enumerate(cols):
        for e in c['covers']:
            by_edge[e].append(i)
    orphan = [e for e, v in by_edge.items() if not v]
    if orphan:
        log(f"    {len(orphan)} trails no column covers; cannot solve")
        return None

    terms = [
        lambda x: sum(x[i] for i in range(len(cols)) if cols[i]['over']),
        lambda x: sum(x),
        lambda x: sum(x[i] * (cols[i]['seconds'] // 60) for i in range(len(cols))),
    ]

    fixed = []
    pick = None
    for k, term in enumerate(terms):
        m = cp_model.CpModel()
        x = [m.NewBoolVar(f'x{i}') for i in range(len(cols))]
        for idx in by_edge.values():
            m.AddBoolOr([x[i] for i in idx])
        for j, val in enumerate(fixed):
            m.Add(terms[j](x) == val)
        m.Minimize(term(x))
        sol = cp_model.CpSolver()
        sol.parameters.max_time_in_seconds = seconds
        sol.parameters.num_search_workers = 4
        st = sol.Solve(m)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return pick
        pick = [i for i in range(len(cols)) if sol.Value(x[i])]
        # Only pin an objective that was proved optimal; pinning a merely
        # feasible value would rule out the better answers below it.
        if st == cp_model.OPTIMAL:
            fixed.append(int(sol.ObjectiveValue()))
        else:
            break
    return pick


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hours', default='8,9,10,11,12,13,14,15,16')
    ap.add_argument('--dirs', nargs='*', default=None,
                    help='where to harvest from (default: docs/data and out/carp*)')
    ap.add_argument('--search', type=float, default=0,
                    help='seconds of local search after the cover')
    ap.add_argument('--exact-max', type=int, default=13)
    ap.add_argument('--emit', default=None)
    A = ap.parse_args()

    dirs = A.dirs or ([os.path.join('docs', 'data')]
                      + sorted(glob.glob(os.path.join('out', 'carp*'))))
    dirs = [d for d in dirs if os.path.isdir(d)]
    nets = {True: Net(ferry=True), False: Net(ferry=False)}
    exs = {k: carp_preset.Expander(v) for k, v in nets.items()}
    routes = harvest(dirs, nets[True])
    print(f"harvested {len(routes)} distinct days from {len(dirs)} directories")

    for h in [int(x) for x in A.hours.split(',')]:
        budget = h * 3600
        best = None
        for ferry in (False, True):
            net, ex = nets[ferry], exs[ferry]
            cols = columns_for(net, routes, budget, budget * HARD_MULTIPLE, ex)
            fit = sum(1 for c in cols if not c['over'])
            # Days inside the budget first.  Minimising the day count over a
            # pool that also holds over-budget days just picks the longest ones
            # -- they cover the most -- and returns an itinerary nobody can
            # walk.  The ceiling is a fallback for tiers where the trail
            # network leaves no alternative, not a licence.
            inside = [c for c in cols if not c['over']]
            pick = solve_cover(net, inside, log=lambda *_: None)
            if pick is not None:
                cols = inside
            else:
                pick = solve_cover(net, cols, log=lambda *_: None)
            if pick is None:
                continue
            chosen = [cols[i] for i in pick]
            over = sum(1 for c in chosen if c['over'])
            key = (over, len(chosen), sum(c['seconds'] for c in chosen))
            print(f"  {h:>2}h {'ferry' if ferry else 'roads':<6} "
                  f"{len(cols):>5} columns ({fit} inside budget)  -> "
                  f"{len(chosen):>3} days, "
                  f"{sum(c['seconds'] for c in chosen) / 3600:>6.1f} h"
                  + (f", {over} over" if over else ""))
            if best is None or key < best[0]:
                best = (key, chosen, net, ferry)
            if not over:
                break            # roads alone suffice; the policy prefers them
        if best is None:
            print(f"  {h:>2}h  no cover found")
            continue

        _, chosen, net, ferry = best
        # Credit every trail to exactly one of the chosen days, preferring a day
        # that already walks it as a leg, then let tighten re-route.
        legs = [c['legs'] for c in chosen]
        credited = [set() for _ in chosen]
        for e in net.required:
            holders = [i for i, c in enumerate(chosen) if e in c['covers']]
            pref = [i for i in holders if e in legs[i]] or holders
            credited[max(pref, key=lambda i: chosen[i]['seconds'])].add(e)
        walks = [{'route': c['route'], 'seconds': c['seconds'],
                  'credited': credited[i]} for i, c in enumerate(chosen)]
        days = carp_credit.tighten(net, walks, budget, A.exact_max,
                                   log=lambda *_: None)
        if A.search:
            days = carp_search.search(net, days, budget, A.exact_max,
                                      A.search, log=lambda *_: None)
        tot = sum(d['seconds'] for d in days)
        pub = os.path.join('docs', 'data', f'preset_supported_{h}h.json')
        note = ''
        if os.path.exists(pub):
            p = json.load(open(pub, encoding='utf-8'))
            ph = sum(a['seconds'] for x in p['days'] for a in x['arcs']) / 3600
            note = (f"   published {p['n_days']}d {ph:.1f}h -> "
                    f"{len(days) - p['n_days']:+d}d {tot / 3600 - ph:+.1f}h")
        print(f"  {h:>2}h FINAL  {len(days)} days, {tot / 3600:.1f} h{note}")
        if A.emit:
            os.makedirs(A.emit, exist_ok=True)
            out = carp_preset.build_preset(net, days, budget)
            carp_preset.check(net, out)
            with open(os.path.join(A.emit,
                                   f'preset_supported_{h}h.json'), 'w',
                      encoding='utf-8') as f:
                json.dump(out, f, indent=2)


if __name__ == '__main__':
    main()
