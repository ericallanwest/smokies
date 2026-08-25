"""Regenerate every published preset.

Why this exists: CP-SAT is seeded and single-threaded, which makes it
reproducible on one platform but not across two.  Presets generated on a
Windows laptop and the manylinux wheel the service runs pick different members
of the same optimal set -- identical objective to the second, different
traversal directions -- so the site and the service disagree on the route while
both being optimal.  Running the batch on Linux removes the difference at its
source; see tools/LOCAL_DEV.md for the WSL setup that makes that free.

The grid is one preset per reachable slider position:

    self-supported   9 day lengths (8..16 h) x 6 resupply windows (4..8, none)
    supported        9 day lengths, no resupply window

Supported is only feasible from 14 h up -- the remotest required trail is 6.6 h
from a road at each end, so a shorter day cannot reach it and return (see
tools/road_bound.py).  The shorter tiers are still attempted, so the batch
reports the gap rather than assuming it.

Writes presets to $OUT (default /workspace/out) and a summary alongside them.
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor

ROOT = os.environ.get('REPO', '/workspace')
# SOLVER can be pointed anywhere, so this runs against a working copy laid out
# however suits, not only the repo layout Cloud Build uploads.
SOLVER = os.environ.get('SOLVER') or os.path.join(
    ROOT, 'solver', 'smokies_circuit_solver_20260509a.py')
SOLVER_DIR = os.path.dirname(SOLVER)
OUT = os.environ.get('OUT', os.path.join(ROOT, 'out'))
WORKERS = int(os.environ.get('WORKERS', str(max(1, (os.cpu_count() or 2) - 1))))
os.makedirs(OUT, exist_ok=True)

TIERS = [int(h) for h in os.environ.get('TIERS', '8,9,10,11,12,13,14,15,16').split(',')]

configs = []
for h in TIERS:
    for r in (None, 4, 5, 6, 7, 8):
        configs.append((f"selfsup_{h}h" + (f"_r{r}" if r else ""),
                        'self-supported', h, r))
    configs.append((f"supported_{h}h", 'supported', h, None))


def run(cfg):
    """One solve.  Returns (label, summary line, wrote?)."""
    label, style, h, r = cfg
    tmp = os.path.join(OUT, f'_{label}.json')
    cmd = [sys.executable, SOLVER, '--max-hours', str(h),
           '--style', style, '--skip-closed', '--json-out', tmp]
    if r:
        cmd += ['--max-resupply-days', str(r)]
    t0 = time.time()
    log = os.path.join(OUT, f'_{label}.log')
    with open(log, 'w', encoding='utf-8') as lf:
        # CSV_PATH in the solver is a bare relative name, so it must run from
        # the directory holding the edge list.
        rc = subprocess.call(cmd, cwd=SOLVER_DIR, stdout=lf,
                             stderr=subprocess.STDOUT)
    secs = int(time.time() - t0)
    wrote = False
    if rc == 0 and os.path.exists(tmp):
        with open(tmp, encoding='utf-8') as f:
            payload = json.load(f)
        # Open only: a closed circuit is now asked for by naming the same start
        # and finish, which is a live solve by definition.
        if payload.get('open'):
            dest = os.path.join(OUT, f'preset_{label}.json')
            with open(dest, 'w', encoding='utf-8') as f:
                json.dump(payload['open'], f, indent=2)
            wrote = True
        os.remove(tmp)
    return label, f"{label:<22} rc={rc} {secs:>4}s {'wrote' if wrote else 'NONE'}", wrote


def main():
    print(f"{len(configs)} configurations on {WORKERS} workers", flush=True)
    summary, n_wrote = [], 0
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        for i, (label, line, wrote) in enumerate(pool.map(run, configs), 1):
            n_wrote += bool(wrote)
            print(f"[{i}/{len(configs)}] {line}", flush=True)
            summary.append(line)

    with open(os.path.join(OUT, 'SUMMARY.txt'), 'w', encoding='utf-8') as f:
        f.write("\n".join(summary) + "\n")
        f.write(f"\n{n_wrote} of {len(configs)} configurations produced a preset\n")
        f.write("BATCH DONE\n")
    print(f"{n_wrote} of {len(configs)} produced a preset", flush=True)
    print("BATCH DONE", flush=True)


if __name__ == '__main__':
    main()
