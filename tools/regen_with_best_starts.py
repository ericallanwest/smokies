"""Regenerate every preset from its day-optimal start.

The default start -- the highest-degree trailhead -- minimises repositioning,
not day count, and day count is what the app optimises for.  Sweeping all 107
candidates per configuration is worth a day or more on most of them.

Only the open walk is published now, so this is one solve per configuration:
the earlier version had to solve some twice because the open and closed
circuits of a configuration could want different starts.

Reads tools/../docs/data/best_starts.json, writes presets to $OUT.
Run tools/build_presets_index.py afterwards so the app can find them.
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
    """'selfsup_12h_r4' -> ('self-supported', 12, '4')"""
    bits = label.split('_')
    style = 'supported' if bits[0] == 'supported' else 'self-supported'
    hours = int(next(b for b in bits if b.endswith('h'))[:-1])
    resupply = next((b[1:] for b in bits if b.startswith('r') and b[1:].isdigit()), '')
    return style, hours, resupply


def run(job):
    """One solve. job = (config label, pinned start or None)."""
    label, start = job
    style, hours, resupply = parse(label)
    tag = f"{label}__{start or 'auto'}"
    tmp = os.path.join(OUT, f'_{tag}.json')
    cmd = [sys.executable, SOLVER, '--max-hours', str(hours), '--style', style,
           '--skip-closed', '--json-out', tmp]
    if resupply:
        cmd += ['--max-resupply-days', resupply]
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

    # best_starts.json is keyed by config label -> {days, default_days, start}.
    # One solve each: only the open walk is published, so there is no second
    # circuit that might want a different start.
    jobs = sorted({(cfg, spec.get('start')) for cfg, spec in best.items() if spec},
                  key=lambda j: (j[0], j[1] or ''))   # unpinned sorts first
    print(f"{len(jobs)} solves for {len(best)} configurations "
          f"on {WORKERS} workers", flush=True)

    results = {}
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        for tag, path in pool.map(run, jobs):
            results[tag] = path

    written = skipped = 0
    for cfg, spec in sorted(best.items()):
        if not spec:
            print(f"  -- {cfg}: no itinerary at any start", flush=True)
            skipped += 1
            continue
        path = results.get(f"{cfg}__{spec.get('start') or 'auto'}")
        if not path:
            print(f"  !! {cfg}: solve produced nothing", flush=True)
            skipped += 1
            continue
        with open(path, encoding='utf-8') as f:
            got = json.load(f).get('open')
        if not got:
            print(f"  !! {cfg}: no open walk in that solve", flush=True)
            skipped += 1
            continue
        if got['n_days'] != spec['days']:
            # The sweep and this run must agree, or the published number is
            # not the one the sweep chose the start for.
            print(f"  !! {cfg}: {got['n_days']} days, sweep said {spec['days']}",
                  flush=True)
        dest = os.path.join(OUT, f'preset_{cfg}.json')
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
