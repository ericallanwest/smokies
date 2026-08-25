"""Choose each supported preset under an explicit precedence.

    SOLVER=... OUT=... python tools/regen_supported.py


A day the hiker cannot finish is worse than a ferry booking, and a ferry
booking is worse than neither.  So:

    1. roads alone, every day inside the budget          <- best
    2. ferry, every day inside the budget
    3. whichever has the fewest over-budget days         <- last resort

Step 3 is capped: a day may run over, but not absurdly.  At 9 h the longest
day comes out at 9.4 h, which is the proven floor and a real trip.  At 8 h the
route has stretches with no pick-up within 30 h of walking, and what comes back
is a 36.8 h day -- not an itinerary anyone can walk, so nothing is published
and the app keeps explaining the gap instead of pretending to fill it.

Step 3 exists because parts of the park cannot be covered inside a short
supported day at any day count -- the AT between Hughes Ridge and Tricorner
Knob needs 9.4 h pick-up to pick-up -- and an itinerary that names its long
days tells the hiker more than no itinerary does.
"""
import json, os, subprocess, time
from concurrent.futures import ProcessPoolExecutor

import sys
SOLVER = os.environ.get('SOLVER') or os.path.join(
    os.environ.get('REPO', '/workspace'), 'solver',
    'smokies_circuit_solver_20260509a.py')
PY_ = sys.executable
OUT = os.environ.get('OUT', os.path.join(os.environ.get('REPO', '/workspace'), 'out'))
FERRY = 'TI051,TI053,TI064,BC090'
os.makedirs(OUT, exist_ok=True)

def solve(h, ferry):
    tmp = os.path.join(OUT, f"_{h}{'f' if ferry else ''}.json")
    cmd = [PY_, SOLVER, '--max-hours', str(h), '--style', 'supported',
           '--skip-closed', '--json-out', tmp]
    if ferry:
        cmd += ['--shuttle-nodes', ferry]
    rc = subprocess.call(cmd, cwd=os.path.dirname(SOLVER),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    got = None
    if rc == 0 and os.path.exists(tmp):
        with open(tmp, encoding='utf-8') as f:
            got = json.load(f).get('open')
        os.remove(tmp)
    return got

# How far past the budget a published day may run.  Anything beyond this is a
# sign the route, not the split, is the problem.
OVERRUN_LIMIT = 1.5

def over(o):
    return len(o.get('days_over_budget', [])) if o else 10**6

def longest_h(o):
    return max(sum(a['seconds'] for a in d['arcs']) for d in o['days']) / 3600

def run(h):
    t0 = time.time()
    road, sea = solve(h, None), None
    if over(road):                       # roads alone cannot keep the budget
        sea = solve(h, FERRY)
    if road is not None and over(road) == 0:
        pick, how = road, 'road only'
    elif sea is not None and over(sea) == 0:
        pick, how = sea, 'ferry'
    else:
        cands = [(over(o), i, o, n) for i, (o, n) in
                 enumerate(((road, 'road only'), (sea, 'ferry'))) if o]
        if not cands:
            return f"{h:>3}h  nothing at all ({int(time.time()-t0)}s)"
        _, _, pick, how = min(cands)
        if longest_h(pick) > h * OVERRUN_LIMIT:
            return (f"{h:>3}h  withheld: longest day {longest_h(pick):.1f} h is "
                    f"more than {OVERRUN_LIMIT:g}x the budget ({int(time.time()-t0)}s)")
        how += f", {over(pick)} day(s) over budget"
    with open(os.path.join(OUT, f'preset_supported_{h}h.json'), 'w',
              encoding='utf-8') as f:
        json.dump(pick, f, indent=2)
    longest = max(sum(a['seconds'] for a in d['arcs'])
                  for d in pick['days']) / 3600
    return (f"{h:>3}h  {pick['n_days']:>3} days  {how:<34} "
            f"longest {longest:4.1f} h  ({int(time.time()-t0)}s)")

if __name__ == '__main__':
    with ProcessPoolExecutor(max_workers=4) as pool:
        for line in pool.map(run, range(8, 17)):
            print(line, flush=True)
    print("DONE")
