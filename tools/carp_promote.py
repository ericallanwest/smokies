"""Pick the best supported preset per tier from several CARP runs.

Phase 3 is a local search with a wall-clock budget, so two runs of the same
tier do not agree -- and running tiers in parallel makes each search weaker,
since the budget is seconds rather than rounds.  Runs therefore accumulate: a
tier keeps whichever run found it the fewest days, and a later batch that does
worse changes nothing.

Selection applies the ferry policy rather than just taking the smallest number.
A road-only itinerary wins ties and near-ties, because a Fontana Lake landing
is an expense and a boat schedule that the hiker has to arrange:

  1. among candidates with no day over budget, prefer road-only;
  2. a ferry candidate has to beat the best clean road-only one by at least
     --ferry-worth days to be taken instead;
  3. if nothing is clean, take fewest over-budget days, then fewest days.

Whether a candidate used the ferry is read from the preset itself, so this
works on any directory of presets whatever produced them.

    python tools/carp_promote.py out/carp_publish out/carp_emit out/carp_road
    python tools/carp_promote.py --into docs/data out/carp_publish
"""
import argparse
import glob
import json
import os
import re
import shutil

PAT = re.compile(r'^preset_supported_(\d+)h\.json$')


def describe(path):
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    hours = int(PAT.match(os.path.basename(path)).group(1))
    walk = sum(a['seconds'] for day in d['days'] for a in day['arcs'])
    ob = d.get('days_over_budget', [])
    return {'path': path, 'hours': hours, 'days': d['n_days'],
            'walk': walk, 'over': len(ob),
            'excess': sum(o['over_by'] for o in ob),
            'worst': max((o['seconds'] for o in ob), default=0),
            'ferry': bool(d.get('ferry_landings'))}


def pick(cands, ferry_worth):
    """The one to publish, by the policy in the module docstring."""
    clean = [c for c in cands if c['over'] == 0]
    if not clean:
        # Counting over-budget days and stopping there is not enough.  At 8 h it
        # once traded four days of about 9.2 h for three days one of which ran
        # 11.95 h, which is fewer broken promises but a much worse one -- and a
        # twelve-hour day is not the trip an eight-hour hiker asked for.  Total
        # excess comes second for the same reason the solver's own day-split
        # ranks it there.
        return min(cands, key=lambda c: (c['over'], c['excess'], c['days'],
                                         c['walk']))
    roads = [c for c in clean if not c['ferry']]
    if not roads:
        return min(clean, key=lambda c: (c['days'], c['walk']))
    best_road = min(roads, key=lambda c: (c['days'], c['walk']))
    ferried = [c for c in clean if c['ferry']]
    if ferried:
        best_ferry = min(ferried, key=lambda c: (c['days'], c['walk']))
        if best_road['days'] - best_ferry['days'] >= ferry_worth:
            return best_ferry
    return best_road


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dirs', nargs='+', help='directories of candidate presets')
    ap.add_argument('--into', default=None,
                    help='copy the winners here (default: report only)')
    ap.add_argument('--ferry-worth', type=int, default=2,
                    help='days a landing must save to be worth arranging')
    A = ap.parse_args()

    by_tier = {}
    for d in A.dirs:
        for fp in sorted(glob.glob(os.path.join(d, 'preset_supported_*h.json'))):
            if PAT.match(os.path.basename(fp)):
                c = describe(fp)
                by_tier.setdefault(c['hours'], []).append(c)

    print(f"{'tier':>5}{'days':>6}{'walk':>9}{'over':>6}  {'access':<7} from")
    for h in sorted(by_tier):
        win = pick(by_tier[h], A.ferry_worth)
        others = ' '.join(f"{c['days']}d" for c in by_tier[h] if c is not win)
        print(f"{h:>4}h{win['days']:>6}{win['walk'] / 3600:>8.1f}h"
              f"{win['over']:>6}  {'ferry' if win['ferry'] else 'roads':<7}"
              f"{os.path.dirname(win['path'])}"
              + (f"   (also saw {others})" if others else ""))
        if A.into:
            os.makedirs(A.into, exist_ok=True)
            dest = os.path.join(A.into, os.path.basename(win['path']))
            # The destination is a legitimate candidate -- passing it in is what
            # stops a worse batch overwriting a better published tier -- so the
            # winner is often the file already sitting there.  Copying it onto
            # itself is an error, not a no-op.
            if os.path.abspath(win['path']) != os.path.abspath(dest):
                shutil.copyfile(win['path'], dest)
    if A.into:
        print(f"copied {len(by_tier)} preset(s) into {A.into}")


if __name__ == '__main__':
    main()
