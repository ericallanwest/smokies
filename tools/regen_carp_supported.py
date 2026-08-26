"""Regenerate the supported presets with CARP, applying the ferry policy.

A Fontana Lake landing is an expense and a boat schedule, and a hiker fit
enough for a 14 h day has no reason to want one.  So the published grid takes
them only where the alternative is not an itinerary at all, and which tiers
those are is measured rather than assumed: every tier is attempted on roads
alone first, and a landing is offered only if what comes back contains days
longer than the hiker asked for.

The bar is deliberately "every day fits the budget" rather than "an itinerary
came back".  CARP declares over-budget days instead of failing, so the second
test is always passed and would never offer the ferry to anyone.  As it stands
the line falls between 13 h and 14 h: on roads alone the remotest required
trail, Lakeshore along Fontana Lake's north shore, needs 13.7 h pick-up to
pick-up, so every tier below 14 h leaves a day over and every tier at or above
it does not.

The search has real variance -- it is a local search with a wall-clock budget,
not an exact method -- so each attempt is run from several seeds and the best
kept.  Restarts are worth more than a proportionally longer single run: at 16 h
one run found 30 days and another 28.

    python tools/regen_carp_supported.py --search 600 --restarts 3
    python tools/regen_carp_supported.py --hours 12 --search 3600 --out out/x
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import carp_preset                                     # noqa: E402
import carp_search                                     # noqa: E402
import carp_supported                                  # noqa: E402
from carp_common import Net                            # noqa: E402

# A tier earns the ferry when roads alone cannot give it a clean itinerary --
# not when roads cannot give it any itinerary at all.  The difference matters:
# CARP declares over-budget days rather than failing, so "did it return
# something" is always yes and would never trigger the fallback.  The test is
# whether every day fits the budget the hiker asked for.

_NET = {}


def net_for(ferry):
    """One graph per worker process, built once."""
    if ferry not in _NET:
        _NET[ferry] = Net(ferry=ferry)
    return _NET[ferry]


def attempt(hours, ferry, search, restarts, exact_max, candidates, seed0):
    """Best of several searches at one budget.

    Returns the days and how many of them exceed the budget.
    """
    net = net_for(ferry)
    budget = hours * 3600
    base = carp_supported.build(net, budget, exact_max, candidates,
                                log=lambda *_: None)
    best = None
    for i in range(max(1, restarts)):
        got = carp_search.search(net, base, budget, exact_max, search,
                                 seed=seed0 + i, log=lambda *_: None)
        key = (len(got), sum(d['seconds'] for d in got))
        if best is None or key < best[0]:
            best = (key, got)
    days = best[1]
    n_over = sum(1 for d in days if d['seconds'] > budget)
    return days, n_over


def run(job):
    hours, A = job
    t0 = time.time()
    days, over = attempt(hours, False, A.search, A.restarts, A.exact_max,
                         A.candidates, A.seed)
    how, ok = 'roads', over == 0
    if not ok:
        # Roads alone leave days nobody asked to walk.  Offer the landings, and
        # keep them only if they actually earn their place.
        fdays, fover = attempt(hours, True, A.search, A.restarts, A.exact_max,
                               A.candidates, A.seed)
        if (fover, len(fdays)) < (over, len(days)):
            days, over, how = fdays, fover, 'ferry'
        ok = over == 0
    net = net_for(how == 'ferry')
    pre = carp_preset.build_preset(net, days, hours * 3600)
    carp_preset.check(net, pre)
    dest = os.path.join(A.out, f'preset_supported_{hours}h.json')
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(pre, f, indent=2)
    walk = sum(a['seconds'] for d in pre['days'] for a in d['arcs']) / 3600
    note = '' if ok else '  days over budget remain'
    return (f"{hours:>4}h {how:<6} {pre['n_days']:>4}d {walk:>7.1f}h "
            f"{len(pre['days_over_budget']):>3} over  "
            f"{int(time.time() - t0):>5}s{note}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hours', default='8,9,10,11,12,13,14,15,16')
    ap.add_argument('--search', type=float, default=600)
    ap.add_argument('--restarts', type=int, default=3)
    ap.add_argument('--exact-max', type=int, default=13)
    ap.add_argument('--candidates', type=int, default=10)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--out', default=os.path.join('out', 'carp_publish'))
    ap.add_argument('--workers', type=int,
                    default=max(1, (os.cpu_count() or 2) - 1))
    A = ap.parse_args()
    os.makedirs(A.out, exist_ok=True)
    tiers = [int(x) for x in A.hours.split(',')]
    print(f"{len(tiers)} tiers on {A.workers} workers, "
          f"{A.restarts} restart(s) x {A.search:.0f}s each", flush=True)
    with ProcessPoolExecutor(max_workers=A.workers) as pool:
        for line in pool.map(run, [(h, A) for h in tiers]):
            print(line, flush=True)
    print("BATCH DONE", flush=True)


if __name__ == '__main__':
    main()
