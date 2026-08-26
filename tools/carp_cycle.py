"""Feed the search's discards back into the pool, and go round again.

The search throws away almost everything it builds.  Across a run it constructs
thousands of days and keeps forty; the rest belonged to solutions that were
rejected as wholes, which says nothing about the individual days.  A day
discarded at 11 h because its neighbours did not work out may be exactly the
day 13 h needs.

So each cycle does three things:

1. search every tier from where it currently stands, several seeds, keeping
   *every* day built rather than only the winners;
2. add all of them to a column bank on disk;
3. re-solve each tier as a set-covering problem over the bank, exactly.

Which is worth doing because of what tools/carp_spread.py found: five seeds per
tier all returned the published day count, with zero variance, and perturbing
thirteen times harder changed nothing -- yet set covering over the pooled days
immediately found six days.  The local search is not short of time, it is short
of moves.  Relocate and swap cannot express "take this day from one run and
that one from another", and the cover can.

Each cycle's searches start from the previous cycle's itineraries, so the bank
grows in a part of the space the earlier ones never visited.  It converges when
a cycle adds columns that change nothing.

    python tools/carp_cycle.py --cycles 3 --seeds 2 --search 240
"""
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import carp_credit                                     # noqa: E402
import carp_search                                     # noqa: E402
from carp_common import Net                            # noqa: E402

_NET = {}


def net_for(ferry):
    if ferry not in _NET:
        _NET[ferry] = Net(ferry=ferry)
    return _NET[ferry]


def construct(job):
    """One randomised construction.  Returns every day it builds.

    A stuck search yields few new columns -- it accepts almost no moves from an
    optimised start, which is what tools/carp_spread.py measured.  Building from
    scratch with a random seed and a random choice among the cheapest insertions
    yields forty-odd days that no previous construction held, which is what the
    pool actually needs.
    """
    import random

    import carp_supported
    hours, seed, exact_max, candidates, ferry = job
    net = net_for(ferry)
    rng = random.Random(seed * 7919 + hours)
    days = carp_supported.build(net, hours * 3600, exact_max, candidates,
                                log=lambda *_: None, rng=rng)
    return hours, seed, len(days), [list(map(list, d['route'])) for d in days]


def explore(job):
    """One tier, one seed.  Returns every distinct day the search touched."""
    hours, seed, data, seconds, exact_max = job
    fp = os.path.join(data, f'preset_supported_{hours}h.json')
    pre = json.load(open(fp, encoding='utf-8'))
    net = net_for(bool(pre.get('ferry_landings')))
    budget = hours * 3600
    walks = carp_credit.days_from_preset(net, pre)
    days = carp_credit.tighten(net, walks, budget, exact_max,
                               log=lambda *_: None, control=True)
    bank = {}
    got = carp_search.search(net, days, budget, exact_max, seconds,
                             seed=seed, log=lambda *_: None, collect=bank)
    return hours, seed, len(got), [list(map(list, r)) for r in bank.values()]


def load_bank(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as f:
        return {frozenset(e for e, _ in r): [(e, d) for e, d in r]
                for r in json.load(f)['routes']}


def save_bank(path, bank, cap):
    routes = list(bank.values())
    # Cap by trail count: a day covering more is a more useful column, and
    # expanding every route against every tier is what costs time downstream.
    if len(routes) > cap:
        routes.sort(key=len, reverse=True)
        routes = routes[:cap]
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'routes': [list(map(list, r)) for r in routes]}, f)
    return len(routes)


def published(data, tiers):
    out = {}
    for h in tiers:
        fp = os.path.join(data, f'preset_supported_{h}h.json')
        if os.path.exists(fp):
            p = json.load(open(fp, encoding='utf-8'))
            out[h] = (p['n_days'],
                      sum(a['seconds'] for d in p['days'] for a in d['arcs']))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hours', default='8,9,10,11,12,13,14,15,16')
    ap.add_argument('--cycles', type=int, default=3)
    ap.add_argument('--seeds', type=int, default=2)
    ap.add_argument('--search', type=float, default=240)
    ap.add_argument('--exact-max', type=int, default=13)
    ap.add_argument('--data', default=os.path.join('docs', 'data'))
    ap.add_argument('--bank', default=os.path.join('out', 'bank.json'))
    ap.add_argument('--max-bank', type=int, default=5000)
    ap.add_argument('--constructions', type=int, default=6,
                    help='randomised builds per tier per cycle')
    ap.add_argument('--candidates', type=int, default=10)
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--python', default=sys.executable)
    A = ap.parse_args()

    tiers = [int(x) for x in A.hours.split(',')]
    tools = os.path.dirname(os.path.abspath(__file__))

    for cycle in range(1, A.cycles + 1):
        print(f"{os.linesep}===== cycle {cycle} of {A.cycles} =====", flush=True)
        before = published(A.data, tiers)
        bank = load_bank(A.bank)
        start = len(bank)

        def absorb(routes):
            n = 0
            for r in routes:
                k = frozenset(e for e, _ in r)
                if k not in bank:
                    bank[k] = [(e, d) for e, d in r]
                    n += 1
            return n

        t0 = time.time()
        cjobs = [(h, cycle * 1000 + s, A.exact_max, A.candidates, True)
                 for h in tiers for s in range(A.constructions)]
        sjobs = [(h, s, A.data, A.search, A.exact_max)
                 for h in tiers for s in range(A.seeds)]
        with ProcessPoolExecutor(max_workers=A.workers) as pool:
            for hours, seed, n, routes in pool.map(construct, cjobs):
                print(f"  {hours:>3}h build {seed}  {n:>3}d  "
                      f"+{absorb(routes)} columns", flush=True)
            for hours, seed, n, routes in pool.map(explore, sjobs):
                print(f"  {hours:>3}h seed  {seed}  {n:>3}d  "
                      f"+{absorb(routes)} columns", flush=True)
        kept = save_bank(A.bank, bank, A.max_bank)
        print(f"  bank {start} -> {len(bank)} days "
              f"({kept} kept), searched in {int(time.time() - t0)}s", flush=True)

        # The cover runs in a separate process because it needs both graphs and
        # a lot of expansion memory, and a fresh one keeps this loop's own
        # caches from growing across cycles.
        rc = subprocess.call([A.python, os.path.join(tools, 'carp_pool.py'),
                              '--hours', A.hours, '--bank', A.bank,
                              '--emit', os.path.join('out', 'cycle')])
        if rc != 0:
            print("  cover failed; stopping", flush=True)
            return
        subprocess.call([A.python, os.path.join(tools, 'carp_promote.py'),
                         '--into', A.data, os.path.join('out', 'cycle'),
                         os.path.join('out', 'pool'),
                         os.path.join('out', 'carp_tight')])
        subprocess.call([A.python, os.path.join(tools, 'build_presets_index.py')])

        after = published(A.data, tiers)
        moved = [f"{h}h {before[h][0]}->{after[h][0]}d" for h in tiers
                 if h in before and after[h][0] != before[h][0]]
        hours_saved = sum(before[h][1] - after[h][1] for h in tiers
                          if h in before) / 3600
        print(f"  cycle {cycle}: "
              + (", ".join(moved) if moved else "no day count changed")
              + f", {hours_saved:+.1f} h walking", flush=True)
        if not moved and abs(hours_saved) < 0.5 and len(bank) == start:
            print("  converged", flush=True)
            return


if __name__ == '__main__':
    main()
