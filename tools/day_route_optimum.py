"""Step 1 of the CARP build order: can exact per-day routing beat the days we
already publish, on their own edge sets?

For each published day, take the required edges it covers and ask the cheapest
possible way to walk exactly those, starting and ending at any pick-up point.
That is a Rural Postman Problem, and with ~10 required edges it is small enough
to solve exactly.

If this cannot beat the current days, no amount of clustering or local search
built on top of it will, and CARP is not worth building.  It can, by about 4%,
which says the overhead lives in which edges each day is given rather than in
the order they are walked -- so the value is in phases 1 and 3.

The graph and the exact router now come from tools/carp_common.py, which fixes
a direction error this file carried at first: it charged each day the cost of
walking *out* to the road in the morning and *in* from it at night.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json                                            # noqa: E402

from carp_common import INF, Net, day_optimum          # noqa: E402

EXACT_MAX = 13


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'docs/data/preset_supported_12h.json'
    net = Net()
    pre = json.load(open(path, encoding='utf-8'))
    print(f"{path}: {pre['n_days']} days")
    print(f"{'day':>4}{'edges':>7}{'now':>9}{'optimal':>10}{'saving':>9}  mode")
    tot_now = tot_opt = 0
    for day in pre['days']:
        eids = sorted({a['edge_id'] for a in day['arcs'] if not a['is_deadhead']})
        now = sum(a['seconds'] for a in day['arcs'])
        opt, _ = day_optimum(net, eids, EXACT_MAX)
        mode = 'exact' if len(eids) <= EXACT_MAX else 'heuristic'
        tot_now += now
        tot_opt += opt if opt < INF else now
        print(f"{day['day']:>4}{len(eids):>7}{now / 3600:>8.2f}h{opt / 3600:>9.2f}h"
              f"{(now - opt) / 3600:>8.2f}h  {mode}")
    print(f"total now {tot_now / 3600:.1f} h, optimal {tot_opt / 3600:.1f} h, "
          f"saving {(tot_now - tot_opt) / 3600:.1f} h "
          f"({100 * (tot_now - tot_opt) / tot_now:.0f}%)")


main()
