"""How long would a supported custom solve take if it covered a shipped bank?

A custom solve currently runs the Eulerian solver and returns about 42 days at
12 h, against a published 36 that CARP produced offline.  Closing that inside
the 45-second budget the app allows means doing the expensive part in advance.

The expensive part is pricing: expanding each banked day against the network to
learn what it costs and which trails it covers.  That depends on the pick-up set
and the route, and on nothing the user chooses -- not the budget, not the
endpoints, not which trails they have already walked -- so it can be computed
once and shipped.  What is left at request time is filtering by cost and solving
the cover, which is the part this measures.

Reported separately, because only the second number has to fit in 45 seconds:

    offline   building and writing the priced bank
    online    load, filter to the budget, cover, credit, expand to a preset

Partial completion makes the cover easier rather than harder -- trails already
walked leave the required set -- so it is measured too, at a quarter done.

    python tools/carp_bench_cover.py --bank out/bank_all.json --hours 12
"""
import argparse
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import carp_credit                                     # noqa: E402
import carp_pool                                       # noqa: E402
import carp_preset                                     # noqa: E402
from carp_common import Net                            # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bank', default=os.path.join('out', 'bank_all.json'))
    ap.add_argument('--hours', default='10,12,14')
    ap.add_argument('--cap', type=float, default=8.0,
                    help='seconds per CP-SAT stage at request time')
    ap.add_argument('--priced', default=os.path.join('out', 'priced_ferry.json'))
    ap.add_argument('--hiked-fraction', type=float, default=0.25)
    A = ap.parse_args()

    t0 = time.time()
    net = Net(ferry=True)
    ex = carp_preset.Expander(net)
    print(f"graph built in {time.time() - t0:.1f}s "
          "(a service would hold this in memory, not rebuild it)")

    with open(A.bank, encoding='utf-8') as f:
        routes = [[(e, d) for e, d in r] for r in json.load(f)['routes']]
    print(f"{len(routes)} banked days")

    t0 = time.time()
    priced = carp_pool.price_all(net, routes, ex)
    t_price = time.time() - t0
    with open(A.priced, 'w', encoding='utf-8') as f:
        json.dump({'columns': [{'route': [list(x) for x in c['route']],
                                'legs': sorted(c['legs']),
                                'seconds': c['seconds'],
                                'covers': sorted(c['covers'])}
                               for c in priced]}, f)
    size = os.path.getsize(A.priced) / 1048576
    print(f"OFFLINE  priced {len(priced)} columns in {t_price:.0f}s, "
          f"{size:.1f} MB on disk")

    t0 = time.time()
    with open(A.priced, encoding='utf-8') as f:
        loaded = [{'route': [(e, d) for e, d in c['route']],
                   'legs': set(c['legs']), 'seconds': c['seconds'],
                   'covers': set(c['covers'])}
                  for c in json.load(f)['columns']]
    t_load = time.time() - t0
    print(f"ONLINE   load {len(loaded)} priced columns: {t_load:.1f}s")

    rng = random.Random(0)
    for h in [int(x) for x in A.hours.split(',')]:
        budget = h * 3600
        for label, hiked in (('all trails', set()),
                             (f"{int(A.hiked_fraction * 100)}% already walked",
                              set(rng.sample(list(net.required),
                                             int(len(net.required)
                                                 * A.hiked_fraction))))):
            need = [e for e in net.required if e not in hiked]
            t0 = time.time()
            cols = [dict(c, over=c['seconds'] > budget)
                    for c in loaded if c['seconds'] <= budget]
            # A cover only has to reach what is left to walk.  Both the cover
            # and the credit pass read the required set off the network, so it
            # is swapped rather than faked -- they also need the real distance
            # tables to route what they choose.
            full_required = net.required
            net.required = need
            pick = carp_pool.solve_cover(net, cols, budget,
                                         log=lambda *_: None, seconds=A.cap)
            t_cover = time.time() - t0
            if pick is None:
                net.required = full_required
                print(f"  {h:>2}h {label:<22} no cover")
                continue
            chosen = [cols[i] for i in pick]
            t0 = time.time()
            legs = [c['legs'] for c in chosen]
            credited = [set() for _ in chosen]
            for e in need:
                holders = [i for i, c in enumerate(chosen) if e in c['covers']]
                pref = [i for i in holders if e in legs[i]] or holders
                credited[max(pref, key=lambda i: chosen[i]['seconds'])].add(e)
            walks = [{'route': c['route'], 'seconds': c['seconds'],
                      'credited': credited[i]} for i, c in enumerate(chosen)]
            try:
                days = carp_credit.tighten(net, walks, budget, 13,
                                           log=lambda *_: None)
            except AssertionError as exc:
                net.required = full_required
                print(f"  {h:>2}h {label:<22} cover {t_cover:5.1f}s, "
                      f"tighten failed: {exc}")
                continue
            finally:
                pass
            net.required = full_required
            t_rest = time.time() - t0
            total = t_cover + t_rest
            print(f"  {h:>2}h {label:<22} {len(days):>3} days   "
                  f"cover {t_cover:5.1f}s + credit {t_rest:4.1f}s "
                  f"= {total:5.1f}s"
                  + ("   FITS 45s" if total < 45 else "   TOO SLOW"))


if __name__ == '__main__':
    main()
