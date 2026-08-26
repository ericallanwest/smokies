"""Stage 0: which tiers still have something to find?

Before building a lower bound to tell us where the slack is, ask the cheap
question: from where each tier stands today, does more searching change
anything, and do different seeds agree about the answer?

A tier where five seeds all land on the same day count is probably near its
floor, and more compute spent there buys nothing.  One where they scatter has
slack the search is not reliably reaching, and is where a long run belongs.

This proves nothing -- agreement between heuristics is not a bound, and five
seeds stuck in the same local optimum look exactly like five seeds at the true
optimum.  But it costs an afternoon rather than three weeks, and if it ranks
the tiers clearly it may answer the question on its own.

Each run continues from the published itinerary rather than starting over, so
what it measures is what more compute would actually buy from here.

    python tools/carp_spread.py --seeds 5 --search 300
"""
import argparse
import json
import os
import statistics
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


def run(job):
    hours, seed, data, search, exact_max = job
    fp = os.path.join(data, f'preset_supported_{hours}h.json')
    pre = json.load(open(fp, encoding='utf-8'))
    net = net_for(bool(pre.get('ferry_landings')))
    budget = hours * 3600
    walks = carp_credit.days_from_preset(net, pre)
    # control=True keeps the published assignment, so seed 0 starts exactly
    # where the published itinerary stands.
    days = carp_credit.tighten(net, walks, budget, exact_max,
                               log=lambda *_: None, control=True)
    t0 = time.time()
    got = carp_search.search(net, days, budget, exact_max, search,
                             seed=seed, log=lambda *_: None)
    return (hours, seed, len(got), sum(d['seconds'] for d in got),
            pre['n_days'], int(time.time() - t0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hours', default='8,9,10,11,12,13,14,15,16')
    ap.add_argument('--seeds', type=int, default=5)
    ap.add_argument('--search', type=float, default=300)
    ap.add_argument('--exact-max', type=int, default=13)
    ap.add_argument('--data', default=os.path.join('docs', 'data'))
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--out', default=os.path.join('out', 'spread.json'))
    A = ap.parse_args()

    tiers = [int(x) for x in A.hours.split(',')]
    jobs = [(h, s, A.data, A.search, A.exact_max)
            for h in tiers for s in range(A.seeds)]
    print(f"{len(jobs)} runs on {A.workers} workers, {A.search:.0f}s each",
          flush=True)

    got = {}
    with ProcessPoolExecutor(max_workers=A.workers) as pool:
        for hours, seed, n, secs, was, took in pool.map(run, jobs):
            got.setdefault(hours, []).append({'seed': seed, 'days': n,
                                              'seconds': secs, 'was': was})
            print(f"  {hours:>3}h seed {seed}  {n:>3}d "
                  f"{secs / 3600:>7.1f}h  (was {was}d)  {took}s", flush=True)

    print()
    print(f"{'tier':>5}{'published':>11}{'best':>6}{'worst':>7}{'spread':>8}"
          f"{'best walk':>11}   verdict")
    for h in sorted(got):
        rows = got[h]
        ds = [r['days'] for r in rows]
        was = rows[0]['was']
        spread = max(ds) - min(ds)
        gain = was - min(ds)
        if gain and spread:
            verdict = 'slack, and seeds disagree -- worth a long run'
        elif gain:
            verdict = 'improves consistently -- worth more time'
        elif spread:
            verdict = 'seeds disagree but none beat published'
        else:
            verdict = 'stable -- more compute here buys nothing'
        print(f"{h:>4}h{was:>10}d{min(ds):>5}d{max(ds):>6}d{spread:>8}"
              f"{min(r['seconds'] for r in rows) / 3600:>10.1f}h   {verdict}")

    with open(A.out, 'w', encoding='utf-8') as f:
        json.dump({'search_seconds': A.search, 'seeds': A.seeds,
                   'tiers': got}, f, indent=1)
    print(f"\nwrote {A.out}")


if __name__ == '__main__':
    main()
