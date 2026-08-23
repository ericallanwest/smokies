"""Regenerate every published preset, inside the service container.

Why this exists: CP-SAT is seeded and single-threaded, which makes it
reproducible on one platform but not across two.  Presets generated on a
Windows laptop and the manylinux wheel the service runs pick different members
of the same optimal set -- identical objective to the second, different
traversal directions -- so the site and the service disagree on the route while
both being optimal.  Running the batch in the deployed image removes the
difference at its source.

Runs as a Cloud Build step against the solver image; writes presets to
$OUT (default /workspace/out) and a summary alongside them.
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.environ.get('REPO', '/workspace')
SOLVER = os.path.join(ROOT, 'solver', 'smokies_circuit_solver_20260509a.py')
OUT = os.environ.get('OUT', os.path.join(ROOT, 'out'))
os.makedirs(OUT, exist_ok=True)

TIERS = [int(h) for h in os.environ.get('TIERS', '8,10,12,14,16').split(',')]

configs = []
for h in TIERS:
    for r in (None, 4, 5, 6, 7, 8):
        for town in (False, True):
            label = f"{h}h" + (f"_r{r}" if r else "") + ("_town" if town else "")
            configs.append((label, h, r, town))

summary = []
for i, (label, h, r, town) in enumerate(configs, 1):
    tmp = os.path.join(OUT, f'_{label}.json')
    cmd = [sys.executable, SOLVER, '--max-hours', str(h), '--json-out', tmp]
    if r:
        cmd += ['--max-resupply-days', str(r)]
    if town:
        cmd += ['--town-nights']
    t0 = time.time()
    log = os.path.join(OUT, f'_{label}.log')
    with open(log, 'w', encoding='utf-8') as lf:
        # CSV_PATH in the solver is a bare relative name, so it must run
        # from the directory holding the edge list.
        rc = subprocess.call(cmd, cwd=os.path.dirname(SOLVER),
                             stdout=lf, stderr=subprocess.STDOUT)
    secs = int(time.time() - t0)
    wrote = []
    if rc == 0 and os.path.exists(tmp):
        with open(tmp, encoding='utf-8') as f:
            payload = json.load(f)
        for kind in ('open', 'closed'):
            if payload.get(kind):
                dest = os.path.join(OUT, f'preset_{kind}_{label}.json')
                with open(dest, 'w', encoding='utf-8') as f:
                    json.dump(payload[kind], f, indent=2)
                wrote.append(kind)
        os.remove(tmp)
    line = f"{label:<16} rc={rc} {secs:>3}s wrote={','.join(wrote) or 'NONE'}"
    print(f"[{i}/{len(configs)}] {line}", flush=True)
    summary.append(line)

with open(os.path.join(OUT, 'SUMMARY.txt'), 'w', encoding='utf-8') as f:
    f.write("\n".join(summary) + "\nBATCH DONE\n")
print("BATCH DONE", flush=True)
