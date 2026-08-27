"""Fill grid cells the solver failed on, using a stricter cell that succeeded.

Some empty cells are the park saying no.  Others are the solver saying "greedy
sweep produced no usable candidate", which is a different statement entirely,
and the grid cannot tell them apart by looking at a missing file.

Monotonicity can.  At Heavy pack and 9 h, the r4 cell produced 86 days and r6
produced 79, while r5 produced nothing -- and an itinerary that resupplies at
least every four days satisfies "at least every five" by construction, so an
answer for r5 was sitting next door the whole time.  Publishing "no itinerary
satisfies this combination" there would have been false.

The dominance rules, each one a containment rather than a heuristic:

  fewer hours   a day inside 8 h is inside 9 h
  tighter r     resupplying every 4 days satisfies every 5, 6, 7, 8, or never
  no ferry      an itinerary that never lands satisfies one that may

Pace is not among them: a different pace is a different set of costs, so
nothing carries across.

Only genuinely empty cells are filled, and days_over_budget is recomputed
against the cell being filled -- a day that ran over at 8 h may sit inside 9 h,
and saying otherwise would misreport the itinerary the hiker is being handed.

**Run this only on a finished grid.**  A cell that has not been built yet looks
exactly like a cell that failed, so on a half-built grid this cheerfully fills
15 h from 13 h and the real 15 h answer, which is better, never gets written.
--min-built refuses to run below a plausible count, and --dry-run is safe at any
time.

    python tools/build_grid.py --out out/grid     # first, to completion
    python tools/fill_dominated.py --grid out/grid
"""
import argparse
import json
import os
import re

SELF = re.compile(r'^preset_selfsup_(\d+)h(?:_r(\d+))?_(\w+)\.json$')
SUPP = re.compile(r'^preset_supported_(\d+)h_(ferry|noferry)_(\w+)\.json$')

HOURS = list(range(8, 17))
RESUPPLY = [4, 5, 6, 7, 8, None]


def rank(r):
    """Tightness of a resupply window; None is the loosest there is."""
    return 99 if r is None else r


def recount(pre, budget):
    """Restate which days run over, for the budget this cell actually has."""
    over = []
    for day in pre['days']:
        secs = sum(a['seconds'] for a in day['arcs'])
        if secs > budget:
            over.append({'day': day['day'], 'seconds': secs,
                         'over_by': secs - budget})
    pre['days_over_budget'] = over
    return pre


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--grid', default=os.path.join('out', 'grid'))
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--min-built', type=int, default=200,
                    help='refuse to fill a grid this incomplete, since an '
                         'unbuilt cell is indistinguishable from a failed one')
    A = ap.parse_args()

    have = set(os.listdir(A.grid)) if os.path.isdir(A.grid) else set()
    n_built = len([f for f in have if f.startswith('preset_')])
    if not A.dry_run and n_built < A.min_built:
        raise SystemExit(
            f"only {n_built} presets in {A.grid}; the grid looks unfinished and "
            f"filling now would freeze worse answers into cells that have not "
            f"been solved yet.  Finish build_grid.py first, or pass "
            f"--min-built {n_built} if you really mean it.")
    paces = sorted({m.group(3) for f in have
                    for m in [SELF.match(f) or SUPP.match(f)] if m})
    filled, still = [], []

    for pace in paces:
        for h in HOURS:
            for r in RESUPPLY:
                name = (f"preset_selfsup_{h}h" + (f"_r{r}" if r else "")
                        + f"_{pace}.json")
                if name in have:
                    continue
                best = None
                for h2 in HOURS:
                    if h2 > h:
                        continue
                    for r2 in RESUPPLY:
                        if rank(r2) > rank(r):
                            continue
                        cand = (f"preset_selfsup_{h2}h"
                                + (f"_r{r2}" if r2 else "") + f"_{pace}.json")
                        if cand not in have:
                            continue
                        with open(os.path.join(A.grid, cand),
                                  encoding='utf-8') as f:
                            d = json.load(f)
                        if best is None or d['n_days'] < best[0]:
                            best = (d['n_days'], cand, d)
                if best is None:
                    still.append(name)
                    continue
                filled.append((name, best[1], best[0]))
                if not A.dry_run:
                    with open(os.path.join(A.grid, name), 'w',
                              encoding='utf-8') as f:
                        json.dump(recount(best[2], h * 3600), f, indent=2)

        for h in HOURS:
            for ferry in (True, False):
                name = (f"preset_supported_{h}h_"
                        f"{'ferry' if ferry else 'noferry'}_{pace}.json")
                if name in have:
                    continue
                best = None
                for h2 in HOURS:
                    if h2 > h:
                        continue
                    # A no-ferry itinerary is valid wherever a ferry is merely
                    # permitted; the reverse is not true.
                    for f2 in ((True, False) if ferry else (False,)):
                        cand = (f"preset_supported_{h2}h_"
                                f"{'ferry' if f2 else 'noferry'}_{pace}.json")
                        if cand not in have:
                            continue
                        with open(os.path.join(A.grid, cand),
                                  encoding='utf-8') as f:
                            d = json.load(f)
                        if best is None or d['n_days'] < best[0]:
                            best = (d['n_days'], cand, d)
                if best is None:
                    still.append(name)
                    continue
                filled.append((name, best[1], best[0]))
                if not A.dry_run:
                    with open(os.path.join(A.grid, name), 'w',
                              encoding='utf-8') as f:
                        json.dump(recount(best[2], h * 3600), f, indent=2)

    for name, src, n in filled:
        print(f"  {name:<44} <- {src}  ({n}d)")
    print(f"{len(filled)} cell(s) filled from a stricter neighbour, "
          f"{len(still)} still empty")
    if still:
        print("still empty (nothing stricter succeeded either):")
        for n in sorted(still)[:20]:
            print(f"  {n}")


if __name__ == '__main__':
    main()
