"""Pick each configuration's day-optimal start from its sweep.

sweep_starts.py writes one sweep_<label>.json per configuration, holding a row
per candidate start.  This reads them all and records the winner, which
regen_with_best_starts.py then solves from.  That step used to be done by hand,
which is a poor place for a hand: the published day count has to be the one the
sweep actually chose the start for, and nothing checks that if the two are
assembled separately.

Ranking matches the solver's own: fewest days, then least total walking.  The
default start (node: null) is kept when nothing beats it, so a configuration
that gains nothing publishes no pin rather than a redundant one.

    python tools/build_best_starts.py --sweeps ~/out/sweeps [--out docs/data/best_starts.json]
"""
import argparse
import glob
import json
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sweeps', required=True,
                    help='directory of sweep_<label>.json files')
    ap.add_argument('--out', default=os.path.join('docs', 'data', 'best_starts.json'))
    ap.add_argument('--merge', action='store_true',
                    help='keep entries already in --out that this run has no '
                         'sweep for, instead of dropping them')
    A = ap.parse_args()

    best = {}
    if A.merge and os.path.exists(A.out):
        with open(A.out, encoding='utf-8') as f:
            best = json.load(f)

    files = sorted(glob.glob(os.path.join(A.sweeps, 'sweep_*.json')))
    if not files:
        raise SystemExit(f"no sweep files in {A.sweeps}")

    total_saved = 0
    for fp in files:
        with open(fp, encoding='utf-8') as f:
            sweep = json.load(f)
        label = sweep['config']
        rows = [r for r in sweep['rows'] if r.get('ok') and r.get('open')]
        if not rows:
            best[label] = {}          # nothing solves at any start
            print(f"  {label:<20} no itinerary at any start")
            continue

        base = next((r for r in sweep['rows'] if r['node'] is None), None)
        base_days = base['open']['days'] if base and base.get('open') else None

        # Fewest days, then least walking -- the solver's own ranking.
        rows.sort(key=lambda r: (r['open']['days'], r['open']['walk']))
        win = rows[0]

        # Only pin when it actually beats the default.  A tie keeps the
        # default, which needs no --start-node and re-solves identically.
        if base_days is not None and win['open']['days'] >= base_days:
            entry = {'days': base_days, 'start': None}
            note = 'default is already best'
        else:
            entry = {'days': win['open']['days'], 'start': win['node']}
            if base_days is not None:
                entry['default_days'] = base_days
                saved = base_days - win['open']['days']
                total_saved += saved
                note = f"{win['node']} saves {saved} day(s) vs {base_days}"
            else:
                note = f"{win['node']} (default start found nothing)"
        best[label] = entry
        print(f"  {label:<20} {entry['days']:>3} days  {note}")

    with open(A.out, 'w', encoding='utf-8') as f:
        json.dump(dict(sorted(best.items())), f, indent=1)
    pinned = sum(1 for v in best.values() if v.get('start'))
    print(f"\nwrote {A.out}: {len(best)} configurations, {pinned} pinned, "
          f"{total_saved} days saved")


if __name__ == '__main__':
    main()
