"""Credit each required trail to a day that already walks it.

A day's route covers the trails it was assigned, but getting between them it
walks a good deal more -- and most of that is required trail too.  At 12 h,
91% of all deadhead lies on trails the itinerary has to cover anyway.  The
hiker is not wasting the effort; the credit simply sits on a different day.

So move the credit to where the walking already happens.  If day 1 climbs
Balsam Mountain to reach the Appalachian Trail, day 1 should be the day that
covers Balsam Mountain, and whichever day was carrying it is left with less to
do -- and a smaller day is one the search can merge away.

The move has to be an assignment change, not a bookkeeping note.  Crediting an
edge to a day that merely passes through it would break the moment that day
re-routed and stopped passing through.  Putting the edge in the day's assigned
set instead obliges the exact router to keep it, and costs that day nothing,
because it walks it already.

Which day should hold the credit, when several walk the same trail?  Whichever
is longest.  Stripping the short days is the point: a day reduced to a couple
of trails is one a neighbour can absorb, while a long day was never going to
merge anyway and carries the extra credit for free.

What it is worth, measured against the presets it was run on: every tier gets
shorter, and 8 h, 9 h and 11 h each lose a day or two.  The gain is largest
where days are short -- at 8 h, taking one trail off a day really does shorten
it, while at 12 h the day still walks through the same country for its other
trails and barely notices.  So the reassignment is worth most exactly where the
itinerary is under the most pressure.

Run with --control to keep the published assignment and search from there, which
is what says whether a gain came from this or merely from more searching.  At
8 h the control returned 62 days and 424.8 h against this pass's 60 and 405.3,
so the reassignment is doing the work.  Note also that no day ever disappears
outright: every day in every published tier carries at least one trail no other
day walks, so this cannot empty one.  What it does is shrink days enough that
the search can merge them.
"""
import sys

from carp_common import day_optimum
from carp_preset import Expander


def direction(net, eid, frm, to):
    """Which orientation of this edge an arc represents, or None."""
    for d in (0, 1):
        t, h, _ = net.legs[eid][d]
        if t == frm and h == to:
            return d
    return None


def days_from_preset(net, preset):
    """Read a published preset back into routes, keeping every traversal.

    The route that comes back is every required trail the day walks, credited
    or not, in the order it walks them -- which is exactly the day's own walk
    relabelled, so its cost does not move.
    """
    required = set(net.required)
    out = []
    for day in preset['days']:
        seen, route = set(), []
        for a in day['arcs']:
            eid = a['edge_id']
            if eid in required and eid not in seen:
                d = direction(net, eid, a['from'], a['to'])
                if d is not None:
                    seen.add(eid)
                    route.append((eid, d))
        out.append({'route': route, 'seconds': sum(a['seconds'] for a in day['arcs']),
                    'credited': {a['edge_id'] for a in day['arcs']
                                 if not a['is_deadhead']}})
    return out


def days_from_routes(net, days):
    """The same, for days still in CARP form: expand, then read back."""
    ex = Expander(net)
    required = set(net.required)
    out = []
    for d in days:
        arcs = ex.day(d['route'])
        seen, route = set(), []
        for a in arcs:
            eid = a['edge_id']
            if eid in required and eid not in seen:
                k = direction(net, eid, a['from'], a['to'])
                if k is not None:
                    seen.add(eid)
                    route.append((eid, k))
        out.append({'route': route, 'seconds': sum(a['seconds'] for a in arcs),
                    'credited': {e for e, _ in d['route']}})
    return out


def tighten(net, walks, budget, exact_max, log=print, control=False):
    """Reassign credit to the longest day that walks each trail, then re-route.

    Takes the output of days_from_preset / days_from_routes -- days whose route
    lists every required trail they touch -- and returns ordinary CARP days,
    each assigned only what it now owns.
    """
    owner = {}
    for i, w in enumerate(walks):
        for eid in w['credited']:
            owner[eid] = i
    walkers = {}
    for i, w in enumerate(walks):
        for eid, _ in w['route']:
            walkers.setdefault(eid, []).append(i)

    moved = 0
    # The control arm: keep the assignment exactly as published, so the only
    # difference between the two runs is where the credit sits.
    for eid, who in ({} if control else walkers).items():
        if len(who) < 2:
            continue
        # The longest day walks the most trail it is not being paid for, and
        # is the least likely to be merged away, so it can hold the credit.
        best = max(who, key=lambda i: (walks[i]['seconds'], -i))
        if owner.get(eid) != best:
            owner[eid] = best
            moved += 1

    days = []
    for i, w in enumerate(walks):
        route = [(e, d) for e, d in w['route'] if owner.get(e) == i]
        if not route:
            continue
        cost, r = day_optimum(net, [e for e, _ in route], exact_max)
        if cost < w['seconds']:
            route = r
        else:
            cost = net.route_cost(route)
            if cost > w['seconds']:
                # The relabelled walk is never worse than the original, so if
                # arithmetic says otherwise the day was not a shortest-path
                # expansion.  Keep the honest number rather than the tidy one.
                pass
        days.append({'route': route, 'seconds': cost, 'over': cost > budget})

    covered = [e for d in days for e, _ in d['route']]
    assert len(covered) == len(set(covered)), 'an edge ended up on two days'
    assert set(covered) == set(net.required), 'tightening lost an edge'
    log(f"    moved {moved} credit(s), {len(walks)} -> {len(days)} days")
    return days


def main():
    import argparse
    import json
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import carp_search
    from carp_common import Net

    ap = argparse.ArgumentParser()
    ap.add_argument('presets', nargs='+')
    ap.add_argument('--search', type=float, default=0)
    ap.add_argument('--exact-max', type=int, default=13)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--control', action='store_true',
                    help='keep the published assignment; search from there')
    ap.add_argument('--emit', default=None)
    A = ap.parse_args()

    import carp_preset
    net_ferry, net_road = Net(ferry=True), Net(ferry=False)
    for path in A.presets:
        pre = json.load(open(path, encoding='utf-8'))
        hours = int(os.path.basename(path).split('_')[-1].rstrip('h.json'))
        net = net_ferry if pre.get('ferry_landings') else net_road
        budget = hours * 3600
        walks = days_from_preset(net, pre)
        was = sum(w['seconds'] for w in walks)
        print(f"{os.path.basename(path)}: {len(walks)} days, {was / 3600:.1f} h")
        days = tighten(net, walks, budget, A.exact_max,
                       control=A.control)
        now = sum(d['seconds'] for d in days)
        print(f"    tightened  {len(days)} days, {now / 3600:.1f} h "
              f"({(now - was) / 3600:+.1f} h)")
        if A.search:
            days = carp_search.search(net, days, budget, A.exact_max,
                                      A.search, seed=A.seed)
        n = len(days)
        tot = sum(d['seconds'] for d in days)
        print(f"    final      {n} days, {tot / 3600:.1f} h  "
              f"({n - len(walks):+d} days, {(tot - was) / 3600:+.1f} h)")
        if A.emit:
            os.makedirs(A.emit, exist_ok=True)
            out = carp_preset.build_preset(net, days, budget)
            carp_preset.check(net, out)
            dest = os.path.join(A.emit, os.path.basename(path))
            with open(dest, 'w', encoding='utf-8') as f:
                json.dump(out, f, indent=2)
            print(f"    emitted    {dest}")


if __name__ == '__main__':
    main()
