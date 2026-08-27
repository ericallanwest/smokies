"""Move a built grid into docs/data, keeping whichever itinerary is better.

build_grid.py gives every cell one solve with one search budget.  The presets
already published are the survivors of days of promote-and-search cycles, and
at almost every supported tier they are still ahead -- 36 days against the
grid's 37 at 12 h, 49 against 50 at 9 h.  Overwriting on the grounds that the
grid is newer would publish worse trips, so each cell is a contest between the
incumbent and the challenger and the incumbent wins ties.

The ranking is carp_promote's, minus the ferry clause: ferry is its own axis
now, so there is no longer a trade to make inside a cell.

    over      a day nobody can walk is the worst thing a preset can contain
    excess    four days at 9.2 h beat three days one of which is 11.95 h
    days      the number the hiker actually cares about
    walk      tie-break

**Legacy aliases.**  The deployed frontend builds `preset_selfsup_12h_r6.json`
from its controls and knows nothing of pace or ferry.  Publishing only the new
names would 404 every request the moment this lands.  So each Standard-pace
cell is also written under its old name -- and for supported, the old name
follows the old publishing policy of preferring roads where roads work, which
is what those files contain today.  The aliases can be deleted once the
frontend reads presets_index.json for the filename, and not before.

    python tools/publish_grid.py --grid out/grid --dry-run
    python tools/publish_grid.py --grid out/grid
"""
import argparse
import json
import os
import re

SELF = re.compile(r'^preset_selfsup_(\d+)h(?:_r(\d+))?_(\w+)\.json$')
SUPP = re.compile(r'^preset_supported_(\d+)h_(ferry|noferry)_(\w+)\.json$')


def emit(src, dest, minify):
    """Copy a preset, optionally without the whitespace.

    288 cells at four paces is 55 MB pretty-printed and 30 MB without the
    indentation, and every rebuild writes another full set into git history.
    Nobody reads a 190 KB itinerary by eye, and the app parses either form
    identically, so the indentation is pure cost.
    """
    with open(src, encoding='utf-8') as f:
        d = json.load(f)
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(d, f, separators=(',', ':')) if minify else json.dump(d, f,
                                                                       indent=2)


def score(path, budget):
    """(over, excess, days, walk) -- lower is better, in that order."""
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    ob = d.get('days_over_budget', [])
    walk = sum(a['seconds'] for day in d['days'] for a in day['arcs'])
    return (len(ob), sum(o['over_by'] for o in ob), d['n_days'], walk)


def legacy_self(h, r):
    return f"preset_selfsup_{h}h" + (f"_r{r}" if r else "") + ".json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--grid', default=os.path.join('out', 'grid'))
    ap.add_argument('--data', default=os.path.join('docs', 'data'))
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--pretty', action='store_true',
                    help='keep the indentation, at roughly twice the bytes')
    ap.add_argument('--no-aliases', action='store_true',
                    help='skip the old filenames; only once the frontend reads '
                         'the index for its filename')
    A = ap.parse_args()

    grid = sorted(f for f in os.listdir(A.grid) if f.startswith('preset_'))
    took, kept, added = [], [], []
    # Standard-pace cells, remembered so the aliases can be written afterwards
    # from whatever actually won each contest.
    std_self, std_supp = {}, {}

    for name in grid:
        m = SELF.match(name)
        if m:
            h, r, pace = int(m.group(1)), m.group(2), m.group(3)
            legacy = legacy_self(h, r)
        else:
            m = SUPP.match(name)
            if not m:
                print(f"  ignoring {name}")
                continue
            h, ferry, pace = int(m.group(1)), m.group(2) == 'ferry', m.group(3)
            legacy = f"preset_supported_{h}h.json"
        budget = h * 3600
        src = os.path.join(A.grid, name)
        dest = os.path.join(A.data, name)

        # The incumbent for a Standard-pace cell is the file the app is serving
        # today, which carries neither pace nor ferry in its name.
        rival = None
        if os.path.exists(dest):
            rival = dest
        elif pace == 'standard' and os.path.exists(os.path.join(A.data, legacy)):
            rival = os.path.join(A.data, legacy)
            # A legacy supported file is one of ferry/noferry, not both, and
            # nothing in the name says which.  Its landings do.
            if SUPP.match(name):
                with open(rival, encoding='utf-8') as f:
                    had = bool(json.load(f).get('ferry_landings'))
                if had != ferry:
                    rival = None

        if rival and score(rival, budget) <= score(src, budget):
            kept.append((name, os.path.basename(rival)))
            winner = rival
        else:
            (took if rival else added).append(name)
            winner = src
        if not A.dry_run:
            emit(winner, dest, not A.pretty)

        if pace == 'standard':
            (std_self if SELF.match(name) else std_supp)[
                (h, r) if SELF.match(name) else (h, ferry)] = winner

    aliases = []
    if not A.no_aliases:
        for (h, r), win in std_self.items():
            aliases.append((legacy_self(h, r), win))
        for h in {h for h, _ in std_supp}:
            # The old policy: roads if roads work, ferry only where they do not.
            road, fer = std_supp.get((h, False)), std_supp.get((h, True))
            win = road if road and score(road, h * 3600)[0] == 0 else (fer or road)
            if win:
                aliases.append((f"preset_supported_{h}h.json", win))
        for name, win in aliases:
            dest = os.path.join(A.data, name)
            if not A.dry_run:
                emit(win, dest, not A.pretty)

    for n, r in kept:
        print(f"  KEEP  {n:<46} (published {r} is no worse)")
    for n in took:
        print(f"  TAKE  {n:<46} (grid beats the published one)")
    print(f"{len(added)} new, {len(took)} replaced, {len(kept)} kept, "
          f"{len(aliases)} legacy aliases"
          + ("   [dry run, nothing written]" if A.dry_run else ""))


if __name__ == '__main__':
    main()
