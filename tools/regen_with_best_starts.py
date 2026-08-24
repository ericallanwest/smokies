"""Regenerate every preset from its day-optimal start.

The default start -- the highest-degree trailhead -- minimises repositioning,
not day count, and day count is what the app optimises for.  Sweeping all 107
candidates per configuration found a better start for 104 of the 114
circuits, worth 120 days in total, and most of the winners walk less as well.

`--start-node` applies to both circuits of a single run, but the sweep ranks
them separately and the winners differ in 10 of 57 configurations.  Those get
solved twice, taking `open` from one run and `closed` from the other.  Runs
are deduplicated, so the common case still costs one solve.

Reads tools/../docs/data/best_starts.json, writes presets to $OUT.
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor

ROOT = os.environ.get('REPO', '/workspace')
SOLVER = os.environ.get('SOLVER') or os.path.join(
    ROOT, 'solver', 'smokies_circuit_solver_20260509a.py')
SOLVER_DIR = os.path.dirname(SOLVER)
OUT = os.environ.get('OUT', os.path.join(ROOT, 'out'))
BEST = os.environ.get('BEST') or os.path.join(
    ROOT, 'docs', 'data', 'best_starts.json')
WORKERS = int(os.environ.get('WORKERS', str(max(1, (os.cpu_count() or 2) - 1))))

os.makedirs(OUT, exist_ok=True)


def parse(label):
    """'12h_r4_town' -> (12, '4', True)"""
    bits = label.split('_')
    hours = int(bits[0][:-1])
    resupply = next((b[1:] for b in bits if b.startswith('r') and b[1:].isdigit()), '')
    return hours, resupply, 'town' in bits


def run(job):
    """One solve. job = (config label, pinned start or None)."""
    label, start = job
    hours, resupply, town = parse(label)
    tag = f"{label}__{start or 'auto'}"
    tmp = os.path.join(OUT, f'_{tag}.json')
    cmd = [sys.executable, SOLVER, '--max-hours', str(hours), '--json-out', tmp]
    if resupply:
        cmd += ['--max-resupply-days', resupply]
    if town:
        cmd += ['--town-nights']
    if start:
        cmd += ['--start-node', start]
    t0 = time.time()
    rc = subprocess.call(cmd, cwd=SOLVER_DIR,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = rc == 0 and os.path.exists(tmp)
    print(f"  {tag:<28} {'ok' if ok else 'FAILED'} ({int(time.time() - t0)}s)",
          flush=True)
    return tag, (tmp if ok else None)


def main():
    with open(BEST, encoding='utf-8') as f:
        best = json.load(f)

    # One job per distinct (config, start); a config whose circuits agree --
    # 47 of 57 -- needs only one.
    jobs = sorted({(cfg, k.get('start'))
                   for cfg, circuits in best.items()
                   for k in circuits.values()},
                  key=lambda j: (j[0], j[1] or ''))   # unpinned sorts first
    print(f"{len(jobs)} solves for {len(best)} configurations "
          f"on {WORKERS} workers", flush=True)

    results = {}
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        for tag, path in pool.map(run, jobs):
            results[tag] = path

    written = skipped = 0
    for cfg, circuits in sorted(best.items()):
        for kind, spec in circuits.items():
            path = results.get(f"{cfg}__{spec.get('start') or 'auto'}")
            if not path:
                print(f"  !! {cfg} {kind}: solve produced nothing", flush=True)
                skipped += 1
                continue
            with open(path, encoding='utf-8') as f:
                payload = json.load(f)
            got = payload.get(kind)
            if not got:
                print(f"  !! {cfg} {kind}: no {kind} circuit in that solve",
                      flush=True)
                skipped += 1
                continue
            want = spec['days']
            if got['n_days'] != want:
                # The sweep and this run must agree, or the published number is
                # not the one the sweep chose the start for.
                print(f"  !! {cfg} {kind}: {got['n_days']} days, sweep said "
                      f"{want}", flush=True)
            dest = os.path.join(OUT, f'preset_{kind}_{cfg}.json')
            with open(dest, 'w', encoding='utf-8') as f:
                json.dump(got, f, indent=2)
            written += 1

    for p in set(results.values()):
        if p and os.path.exists(p):
            os.remove(p)
    print(f"wrote {written} presets, skipped {skipped}", flush=True)
    print("REGEN DONE", flush=True)


if __name__ == '__main__':
    main()
