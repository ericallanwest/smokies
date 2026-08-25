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

Supported is only feasible from 14 h up on roads alone -- the remotest required
trail is 6.6 h from tarmac at each end, so a shorter day cannot reach it and
return (see tools/road_bound.py).  The Fontana Lake ferry landings drop that
floor to 9.4 h.

A ferry is an expense and a schedule, though, and a hiker fit enough for a 16 h
day has no reason to want one.  So the published grid takes them only where the
alternative is no itinerary at all: 10-13 h ship ferry-enabled, 14-16 h ship
road-only, and a custom solve lets the hiker pick landings for themselves.
Each tier is attempted road-only first, so that policy is applied from what the
solver actually returns rather than from a hardcoded cutoff.

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

# Every landing, used only as a fallback -- see the module docstring.
FERRY = 'TI051,TI053,TI064,BC090'

configs = []
for h in TIERS:
    for r in (None, 4, 5, 6, 7, 8):
        configs.append((f"selfsup_{h}h" + (f"_r{r}" if r else ""),
                        'self-supported', h, r))
    configs.append((f"supported_{h}h", 'supported', h, None))


def solve(label, style, h, r, ferry):
    """One invocation.  Returns the parsed open walk, or None."""
    tag = label + ('_ferry' if ferry else '')
    tmp = os.path.join(OUT, f'_{tag}.json')
    cmd = [sys.executable, SOLVER, '--max-hours', str(h),
           '--style', style, '--skip-closed', '--json-out', tmp]
    if r:
        cmd += ['--max-resupply-days', str(r)]
    if ferry:
        cmd += ['--shuttle-nodes', ferry]
    with open(os.path.join(OUT, f'_{tag}.log'), 'w', encoding='utf-8') as lf:
        # CSV_PATH in the solver is a bare relative name, so it must run from
        # the directory holding the edge list.
        rc = subprocess.call(cmd, cwd=SOLVER_DIR, stdout=lf,
                             stderr=subprocess.STDOUT)
    got = None
    if rc == 0 and os.path.exists(tmp):
        with open(tmp, encoding='utf-8') as f:
            got = json.load(f).get('open')
        os.remove(tmp)
    return got


def run(cfg):
    """One configuration.  Returns (label, summary line, wrote?)."""
    label, style, h, r = cfg
    t0 = time.time()
    got = solve(label, style, h, r, None)
    note = ''
    if got is None and style == 'supported':
        # Nothing on roads alone: this is the case a ferry is for.
        got = solve(label, style, h, r, FERRY)
        note = ' via ferry' if got else ''
    secs = int(time.time() - t0)
    if got is None:
        return label, f"{label:<22} {secs:>4}s NONE", False
    dest = os.path.join(OUT, f'preset_{label}.json')
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(got, f, indent=2)
    return label, f"{label:<22} {secs:>4}s {got['n_days']:>3}d{note}", True


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
