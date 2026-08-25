"""Find the day-optimal start node for one configuration.

The default start is the highest-degree trailhead, which minimises
repositioning but is not chosen for day count -- and day count is the app's
primary objective.  Where the walk begins does not change which arcs it covers,
only where the day boundaries fall -- and boundaries have to land on legal
overnights, so moving the start reshuffles them and can save a day.

Only the open walk is swept and published now; a closed circuit is asked for by
naming the same start and finish, which is a live solve by definition.

Sweeping is worth much less under --style supported: every day there is placed
independently between roads, so only day 1 is anchored by the start.  Sweep
self-supported, and take the default for supported.

The sweep is therefore worth running, but only offline: CP-SAT does not depend
on the start, yet the CLI re-runs it per invocation, so each candidate costs a
full solve.  107 candidates is roughly 45 minutes serial, which is fine on a
build machine and impossible inside a live request budget.

Runs candidates in parallel across the builder's cores; each solve is
single-threaded by design (CPSAT_SEED plus num_search_workers=1), so
parallelism here does not disturb reproducibility.
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor

ROOT = os.environ.get('REPO', '/workspace')
# SOLVER and STARTS can be pointed anywhere, so this runs against a working
# copy laid out however suits, not only the repo layout Cloud Build uploads.
SOLVER = os.environ.get('SOLVER') or os.path.join(
    ROOT, 'solver', 'smokies_circuit_solver_20260509a.py')
SOLVER_DIR = os.path.dirname(SOLVER)
OUT = os.environ.get('OUT', os.path.join(ROOT, 'out'))
STARTS = os.environ.get('STARTS') or os.path.join(
    ROOT, 'docs', 'data', 'start_points.json')

MAX_HOURS = os.environ.get('MAX_HOURS', '12')
RESUPPLY = os.environ.get('RESUPPLY', '')          # '' or e.g. '6'
STYLE = os.environ.get('STYLE', 'self-supported')  # or 'supported'
WORKERS = int(os.environ.get('WORKERS', str(max(1, (os.cpu_count() or 2) - 1))))

os.makedirs(OUT, exist_ok=True)


def solve(node_id):
    """Return per-circuit stats for one pinned start, or None if it failed."""
    tmp = os.path.join(OUT, f'_sweep_{node_id or "auto"}.json')
    # --skip-closed: only the open walk is published, and building the closed
    # circuit costs a second day split, which is most of the run.  Over a
    # 108-candidate sweep that is half the wall clock for nothing.
    cmd = [sys.executable, SOLVER, '--max-hours', MAX_HOURS, '--style', STYLE,
           '--skip-closed', '--json-out', tmp]
    if RESUPPLY:
        cmd += ['--max-resupply-days', RESUPPLY]
    if node_id:
        cmd += ['--start-node', node_id]
    t0 = time.time()
    rc = subprocess.call(cmd, cwd=SOLVER_DIR,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if rc != 0 or not os.path.exists(tmp):
        return {'node': node_id, 'ok': False, 'seconds': int(time.time() - t0)}
    with open(tmp, encoding='utf-8') as f:
        payload = json.load(f)
    os.remove(tmp)
    row = {'node': node_id, 'ok': True, 'seconds': int(time.time() - t0)}
    for kind in ('open',):
        o = payload.get(kind)
        row[kind] = None if not o else {
            'days': o['n_days'],
            'walk': sum(a['seconds'] for d in o['days'] for a in d['arcs']),
            'stops': len(o.get('resupply_plan') or []),
        }
    return row


def main():
    with open(STARTS, encoding='utf-8') as f:
        points = json.load(f)
    # None first: the current default, so every candidate has a baseline to
    # be judged against in the same run and on the same machine.
    candidates = [None] + [p['id'] for p in points]
    style_sfx = 'supported' if STYLE == 'supported' else 'selfsup'
    label = f"{style_sfx}_{MAX_HOURS}h" + (f'_r{RESUPPLY}' if RESUPPLY else '')
    print(f"sweeping {len(candidates)} starts for {label} on {WORKERS} workers",
          flush=True)

    rows = []
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        for i, row in enumerate(pool.map(solve, candidates), 1):
            rows.append(row)
            tag = row['node'] or '(default)'
            if row['ok']:
                o = row.get('open')
                print(f"[{i}/{len(candidates)}] {tag:<8} "
                      f"days {o['days'] if o else '-'}  ({row['seconds']}s)",
                      flush=True)
            else:
                print(f"[{i}/{len(candidates)}] {tag:<8} FAILED ({row['seconds']}s)",
                      flush=True)

    with open(os.path.join(OUT, f'sweep_{label}.json'), 'w', encoding='utf-8') as f:
        json.dump({'config': label, 'rows': rows}, f, indent=1)

    base = next(r for r in rows if r['node'] is None)
    for kind in ('open',):
        got = [r for r in rows if r['ok'] and r.get(kind)]
        if not got:
            continue
        # Fewest days, then least walking -- the same order the solver ranks by.
        got.sort(key=lambda r: (r[kind]['days'], r[kind]['walk']))
        best = got[0]
        b = base.get(kind)
        print(f"\n{kind}: default {b['days'] if b else '-'} days, "
              f"best {best[kind]['days']} days at {best['node'] or '(default)'}",
              flush=True)
        for r in got[:8]:
            print(f"    {r['node'] or '(default)':<8} {r[kind]['days']:>3} days "
                  f"{r[kind]['walk']:>10,}s", flush=True)
    print("SWEEP DONE", flush=True)


if __name__ == '__main__':
    main()
