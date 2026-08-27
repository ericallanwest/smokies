"""Make the grid monotonic: fill cells the solver failed on, and fix inversions.

Some empty cells are the park saying no.  Others are the solver saying "greedy
sweep produced no usable candidate", which is a different statement entirely,
and the grid cannot tell them apart by looking at a missing file.

Monotonicity can.  At Heavy pack and 9 h, the r4 cell produced 86 days and r6
produced 79, while r5 produced nothing -- and an itinerary that resupplies at
least every four days satisfies "at least every five" by construction, so an
answer for r5 was sitting next door the whole time.  Publishing "no itinerary
satisfies this combination" there would have been false.

The same containment catches a subtler wrong: a cell that *was* built, but
worse than one whose constraints are tighter.  Fast pack at 12 h came out at 33
days with the ferry allowed and 32 without -- yet every no-ferry itinerary is
legal when the ferry is merely permitted, so the ferry cell can never really be
worse.  It is search noise, and published side by side it reads as a bug.
Whichever answer is better belongs in both cells.

That replacement applies to **supported cells only**.  A self-supported preset
is exactly what the deployed service returns at those settings, arc for arc --
solver-health.yml asserts it, and it is why those presets can be trusted at
all.  Moving the 12 h r4 itinerary into the unlimited cell buys one day and
costs that: the service, asked for 12 h with no resupply limit, would return
something else and CI would rightly fail.  A day is not worth the guarantee.
Empty self-supported cells are still filled, because a cell with no file has
no parity to lose and "no itinerary exists" would be a lie.

The dominance rules, each one a containment rather than a heuristic:

  fewer hours   a day inside 8 h is inside 9 h
  tighter r     resupplying every 4 days satisfies every 5, 6, 7, 8, or never
  no ferry      an itinerary that never lands satisfies one that may

Pace is not among them: a different pace is a different set of costs, so
nothing carries across.

days_over_budget is recomputed against the cell being filled -- a day that ran
over at 8 h may sit inside 9 h -- and the comparison uses that recount, ranking
on (over-budget days, total excess, days, walking) exactly as publication does.
Ranking on day count alone would trade four days at 9.2 h for three days one of
which is 11.95 h, which is fewer broken promises but a much worse one.

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


def self_name(h, r, pace):
    return f"preset_selfsup_{h}h" + (f"_r{r}" if r else "") + f"_{pace}.json"


def supp_name(h, ferry, pace):
    return f"preset_supported_{h}h_{'ferry' if ferry else 'noferry'}_{pace}.json"


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


def score(pre):
    """(over, excess, days, walk) -- lower is better, in that order.

    Call only after recount() against the receiving cell's budget.
    """
    ob = pre['days_over_budget']
    return (len(ob), sum(o['over_by'] for o in ob), pre['n_days'],
            sum(a['seconds'] for d in pre['days'] for a in d['arcs']))


def load(grid, name, budget):
    with open(os.path.join(grid, name), encoding='utf-8') as f:
        return recount(json.load(f), budget)


def resolve(grid, have, name, dominators, budget):
    """The best itinerary legal in this cell: its own, or a stricter cell's."""
    best = None
    for cand in ([name] if name in have else []) + [
            c for c in dominators if c in have]:
        d = load(grid, cand, budget)
        s = score(d)
        if best is None or s < best[0]:
            best = (s, cand, d)
    return best


def cells():
    """Every cell in the grid, with the cells whose answers are legal in it."""
    for pace in ('heavy', 'standard', 'strong', 'fast'):
        for h in HOURS:
            for r in RESUPPLY:
                yield (self_name(h, r, pace), h, r,
                       [self_name(h2, r2, pace)
                        for h2 in HOURS if h2 <= h
                        for r2 in RESUPPLY if rank(r2) <= rank(r)
                        and self_name(h2, r2, pace) != self_name(h, r, pace)])
            for ferry in (True, False):
                # A no-ferry itinerary is valid wherever a ferry is merely
                # permitted; the reverse is not true.
                yield (supp_name(h, ferry, pace), h, None,
                       [supp_name(h2, f2, pace)
                        for h2 in HOURS if h2 <= h
                        for f2 in ((True, False) if ferry else (False,))
                        if supp_name(h2, f2, pace) != supp_name(h, ferry, pace)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--grid', default=os.path.join('out', 'grid'))
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-replace', action='store_true',
                    help='only fill empty cells; leave a built-but-worse '
                         'supported cell showing more days than a stricter one '
                         'next to it')
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

    filled, fixed, still = [], [], []
    for name, hours, rmax, dominators in cells():
        best = resolve(A.grid, have, name, dominators, hours * 3600)
        if best is None:
            still.append(name)
            continue
        s, src, pre = best
        if src == name:
            continue
        if name in have:
            # Only where the stricter cell is better on something a hiker would
            # notice.  Ranking down to walking seconds would also replace 28
            # cells that tie on days and differ by a fraction of a percent,
            # collapsing distinct configurations into copies of each other --
            # someone who asked for no resupply limit should not silently be
            # handed the every-four-days itinerary to save 20 minutes.
            #
            # And never a self-supported cell that already has an answer: that
            # file is the service's own output at those settings, which is a
            # stronger property than one fewer day.  See the module docstring.
            if (A.no_replace or SELF.match(name)
                    or s[:3] >= score(load(A.grid, name, hours * 3600))[:3]):
                continue
            fixed.append((name, src, s[2]))
        else:
            filled.append((name, src, s[2]))
        # The file has to claim the window its filename promises, not the
        # tighter one it was solved under.  The itinerary satisfies both by
        # construction, and check 2e re-derives the window from the arcs, so
        # this cannot launder a violation.
        if rmax is not None or 'max_days_between_resupply' in pre:
            pre['max_days_between_resupply'] = rmax
        if not A.dry_run:
            with open(os.path.join(A.grid, name), 'w', encoding='utf-8') as f:
                json.dump(pre, f, indent=2)

    for name, src, n in filled:
        print(f"  FILL    {name:<44} <- {src}  ({n}d)")
    for name, src, n in fixed:
        print(f"  REPLACE {name:<44} <- {src}  ({n}d)")
    print(f"{len(filled)} empty cell(s) filled, {len(fixed)} built cell(s) "
          f"replaced by a stricter one that did better, {len(still)} still empty"
          + ("   [dry run, nothing written]" if A.dry_run else ""))
    if still:
        print("still empty (nothing stricter succeeded either):")
        for n in sorted(still)[:24]:
            print(f"  {n}")


if __name__ == '__main__':
    main()
