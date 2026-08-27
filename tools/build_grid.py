"""Build every default itinerary the app offers, at every pace.

The grid was nine day-lengths: 54 self-supported and 9 supported.  Adding the
four paces the app already offers as buttons, and making the ferry an explicit
yes or no rather than something the publishing policy decided, gives

    self-supported   9 hours x 6 resupply windows x 4 paces   = 216
    supported        9 hours x 2 ferry x 4 paces              =  72

Every cell is a solve, so this runs for hours and is meant to be left alone.
It is idempotent: a cell whose file already exists is skipped, so an interrupted
run is resumed by starting it again.

The two halves are built by different machinery, for the reason they always
have been.  A self-supported hiker sleeps where they stop, so their days chain
and the Eulerian solver is correct for them; each cell is one invocation.  A
supported hiker's days do not chain, so those come from the CARP tools: price
the banked days against this pace and pick-up set, cover, credit, search.

Pace is what makes the bank worth having.  A route is a route at any pace --
only its price changes, and pricing 14,000 days takes a second -- so Heavy pack
inherits every day ever built for Standard rather than starting from nothing.

Nothing is published from here.  Everything lands in --out, to be validated and
promoted separately, so a bad run cannot reach the site.

    python tools/build_grid.py --out out/grid --search 300
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
import carp_pool                                       # noqa: E402
import carp_preset                                     # noqa: E402
import carp_search                                     # noqa: E402
import carp_supported                                  # noqa: E402
from carp_common import PACES, Net                     # noqa: E402

HOURS = list(range(8, 17))
RESUPPLY = [None, 4, 5, 6, 7, 8]
FERRY_NODES = 'TI051,TI053,TI064,BC090'
HARD_MULTIPLE = 1.25

_CACHE = {}


def net_for(pace_name, ferry):
    key = (pace_name, ferry)
    if key not in _CACHE:
        _CACHE[key] = Net(ferry=ferry, pace=PACES[pace_name])
    return _CACHE[key]


def selfsup_name(h, r, pace):
    return f"preset_selfsup_{h}h" + (f"_r{r}" if r else "") + f"_{pace}.json"


def supported_name(h, ferry, pace):
    return f"preset_supported_{h}h_{'ferry' if ferry else 'noferry'}_{pace}.json"


def run_selfsup(job):
    """One self-supported cell, straight from the solver."""
    h, r, pace, out, solver_dir = job
    dest = os.path.join(out, selfsup_name(h, r, pace))
    if os.path.exists(dest):
        return f"  {os.path.basename(dest):<44} already built"
    tmp = dest + '.tmp'
    cmd = [sys.executable, os.path.join(solver_dir,
                                        'smokies_circuit_solver_20260509a.py'),
           '--max-hours', str(h), '--style', 'self-supported',
           '--skip-closed', '--json-out', tmp]
    if r:
        cmd += ['--max-resupply-days', str(r)]
    if pace != 'standard':
        v0, k, peak = PACES[pace]
        cmd += ['--tobler-v0', str(v0), '--tobler-k', str(k),
                '--tobler-peak', str(peak)]
    t0 = time.time()
    rc = subprocess.call(cmd, cwd=solver_dir,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if rc != 0 or not os.path.exists(tmp):
        return f"  {os.path.basename(dest):<44} NONE ({int(time.time() - t0)}s)"
    with open(tmp, encoding='utf-8') as f:
        got = json.load(f).get('open')
    os.remove(tmp)
    if not got:
        return f"  {os.path.basename(dest):<44} NONE ({int(time.time() - t0)}s)"
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(got, f, indent=2)
    return (f"  {os.path.basename(dest):<44} {got['n_days']:>3}d "
            f"({int(time.time() - t0)}s)")


def run_supported(job):
    """One supported cell: cover the bank, credit, then search."""
    h, ferry, pace, out, bank_path, search, exact_max = job
    dest = os.path.join(out, supported_name(h, ferry, pace))
    if os.path.exists(dest):
        return f"  {os.path.basename(dest):<44} already built"
    t0 = time.time()
    net = net_for(pace, ferry)
    ex = carp_preset.Expander(net)
    budget = h * 3600

    with open(bank_path, encoding='utf-8') as f:
        routes = [[(e, d) for e, d in r] for r in json.load(f)['routes']]
    priced = carp_pool.price_all(net, routes, ex)
    cols = [dict(c, over=c['seconds'] > budget)
            for c in priced if c['seconds'] <= budget * HARD_MULTIPLE]
    inside = [c for c in cols if not c['over']]
    pick = carp_pool.solve_cover(net, inside, budget, log=lambda *_: None)
    if pick is not None:
        cols = inside
    else:
        pick = carp_pool.solve_cover(net, cols, budget, log=lambda *_: None)
    if pick is None:
        # No set of banked days covers the park at this pace and pick-up set.
        # A construction from scratch is the fallback, and if that cannot do it
        # either the cell has no itinerary and the index will say so.
        days = carp_supported.build(net, budget, exact_max, 10,
                                    log=lambda *_: None)
    else:
        chosen = [cols[i] for i in pick]
        legs = [c['legs'] for c in chosen]
        credited = [set() for _ in chosen]
        for e in net.required:
            holders = [i for i, c in enumerate(chosen) if e in c['covers']]
            if not holders:
                return f"  {os.path.basename(dest):<44} NONE (uncoverable)"
            pref = [i for i in holders if e in legs[i]] or holders
            credited[max(pref, key=lambda i: chosen[i]['seconds'])].add(e)
        walks = [{'route': c['route'], 'seconds': c['seconds'],
                  'credited': credited[i]} for i, c in enumerate(chosen)]
        days = carp_credit.tighten(net, walks, budget, exact_max,
                                   log=lambda *_: None)
    if search:
        days = carp_search.search(net, days, budget, exact_max, search,
                                  log=lambda *_: None)
    pre = carp_preset.build_preset(net, days, budget)
    carp_preset.check(net, pre)
    worst = max((o['seconds'] for o in pre['days_over_budget']), default=0)
    if worst > budget * HARD_MULTIPLE:
        return (f"  {os.path.basename(dest):<44} NONE "
                f"(worst day {worst / 3600:.1f}h, past the ceiling)")
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(pre, f, indent=2)
    ob = len(pre['days_over_budget'])
    return (f"  {os.path.basename(dest):<44} {pre['n_days']:>3}d"
            + (f" {ob} over" if ob else "       ")
            + f" ({int(time.time() - t0)}s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join('out', 'grid'))
    ap.add_argument('--bank', default=os.path.join('out', 'bank_all.json'))
    ap.add_argument('--search', type=float, default=300)
    ap.add_argument('--exact-max', type=int, default=13)
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--selfsup-workers', type=int, default=7)
    ap.add_argument('--only', choices=['selfsup', 'supported'], default=None)
    A = ap.parse_args()
    # The solver runs with cwd=solver/, because its CSV path is a bare name, so
    # anything handed to it has to be absolute or it writes somewhere nobody is
    # looking -- which is exactly what "NONE" meant the first time this ran.
    A.out = os.path.abspath(A.out)
    A.bank = os.path.abspath(A.bank)
    os.makedirs(A.out, exist_ok=True)
    solver_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'solver')

    if A.only != 'supported':
        jobs = [(h, r, p, A.out, solver_dir)
                for p in PACES for h in HOURS for r in RESUPPLY]
        print(f"SELF-SUPPORTED: {len(jobs)} cells on {A.selfsup_workers} "
              f"workers", flush=True)
        t0 = time.time()
        with ProcessPoolExecutor(max_workers=A.selfsup_workers) as pool:
            for line in pool.map(run_selfsup, jobs):
                print(line, flush=True)
        print(f"self-supported done in {int(time.time() - t0)}s", flush=True)

    if A.only != 'selfsup':
        jobs = [(h, f, p, A.out, A.bank, A.search, A.exact_max)
                for p in PACES for h in HOURS for f in (True, False)]
        print(f"SUPPORTED: {len(jobs)} cells on {A.workers} workers",
              flush=True)
        t0 = time.time()
        with ProcessPoolExecutor(max_workers=A.workers) as pool:
            for line in pool.map(run_supported, jobs):
                print(line, flush=True)
        print(f"supported done in {int(time.time() - t0)}s", flush=True)

    built = len([f for f in os.listdir(A.out) if f.startswith('preset_')])
    print(f"GRID DONE: {built} presets in {A.out}", flush=True)


if __name__ == '__main__':
    main()
