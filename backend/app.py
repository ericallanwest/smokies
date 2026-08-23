"""Smokies solve service: run the circuit solver on demand and stream progress.

POST /solve with a JSON body of solver parameters. The response is a streamed
NDJSON body: zero or more {"type": "progress", "pct": N, "label": "..."} lines
(forwarded live from the solver's PROGRESS output) followed by exactly one
{"type": "result", ...envelope} or {"type": "error", "message": "..."} line.
The result envelope's open/closed objects use the same schema as the
pre-solved preset JSONs, so the web app renders them unchanged.

The solver runs as a subprocess in an isolated scratch directory (a copy of
the edge list only), so its output files never touch the repo. Results are
cached in memory by parameter hash: repeat requests return instantly.

Local run:   uvicorn app:app --port 8080   (from this directory)
Container:   see Dockerfile / README.md
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

HERE = os.path.dirname(os.path.abspath(__file__))
SOLVER_DIR = os.environ.get('SOLVER_DIR', os.path.join(HERE, '..', 'solver'))
SOLVER_PY = os.path.join(SOLVER_DIR, 'smokies_circuit_solver_20260509a.py')
EDGE_CSV = os.path.join(SOLVER_DIR, 'smokies_edge_list_20260509a.csv')
# Slope histograms, needed only when a request overrides the Tobler parameters.
# Looked for beside the solver first, then in the published data directory.
PROFILES = next(
    (c for c in (os.path.join(SOLVER_DIR, 'segment_profiles.json'),
                 os.path.join(HERE, '..', 'docs', 'data', 'segment_profiles.json'))
     if os.path.exists(c)), None)

# Scratch working directory: the solver reads its edge list from cwd and
# writes itinerary/preset files there, so give it a private sandbox.
WORKDIR = tempfile.mkdtemp(prefix='smokies_solver_')
shutil.copy(EDGE_CSV, WORKDIR)
if PROFILES:
    shutil.copy(PROFILES, WORKDIR)

# One solve at a time: the solver is CPU-bound and a Cloud Run instance has
# one core by default. Concurrent requests queue here (each solve is ~5-15 s).
_solve_lock = asyncio.Lock()
_cache: dict[str, dict] = {}

# --- Abuse and overload limits -------------------------------------------
# /solve is an unauthenticated CPU endpoint linked from a public page, and
# --max-instances caps the bill, not the abuse: one client in a loop can hold
# the solve lock indefinitely and lock everyone else out.
#
# The queue cap matters more than the per-IP cap.  Solves are serialised and
# take 15-25 s, so without a depth limit the tenth caller waits three minutes
# and times out anyway -- having queued behind nine solves that already
# finished.  Refusing immediately is both cheaper and more honest.
#
# Both counters are per-instance.  With --max-instances 2 that is a factor of
# two of slack, which is the right trade for keeping the service stateless;
# tightening it would mean a shared store for a service whose whole point is
# that it holds nothing.
# Read from the environment so they can be retuned on a running service with
# `gcloud run services update --set-env-vars`, without a rebuild.
MAX_QUEUE_DEPTH = int(os.environ.get('MAX_QUEUE_DEPTH', 4))
RATE_LIMIT_SOLVES = int(os.environ.get('RATE_LIMIT_SOLVES', 12))
RATE_LIMIT_WINDOW = float(os.environ.get('RATE_LIMIT_WINDOW', 600))
_waiting = 0
_recent: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    # Cloud Run terminates TLS upstream, so request.client is the proxy; the
    # original address is the first hop in X-Forwarded-For.
    fwd = request.headers.get('x-forwarded-for', '')
    return fwd.split(',')[0].strip() or (request.client.host if request.client else '?')


def _rate_limited(ip: str) -> float:
    """Seconds to wait before this client may solve again, 0 if allowed."""
    now = time.monotonic()
    hits = [t for t in _recent.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
    _recent[ip] = hits
    if len(hits) >= RATE_LIMIT_SOLVES:
        return round(RATE_LIMIT_WINDOW - (now - hits[0]), 1)
    hits.append(now)
    # Opportunistic sweep: without it the map grows with every distinct caller.
    if len(_recent) > 2048:
        for k in [k for k, v in _recent.items() if not v or now - v[-1] > RATE_LIMIT_WINDOW]:
            _recent.pop(k, None)
    return 0.0

app = FastAPI(title='Smokies solve service')
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        'https://ericallanwest.github.io',
        'http://localhost:8000',
        'http://127.0.0.1:8000',
    ],
    allow_methods=['GET', 'POST'],
    allow_headers=['*'],
)


class SolveRequest(BaseModel):
    max_hours: float = Field(12.0, ge=4, le=24)
    max_resupply_days: int | None = Field(None, ge=1, le=30)
    town_nights: bool = False
    hiked: list[str] = Field(default_factory=list, max_length=1000)
    time_budget: float = Field(45.0, ge=5, le=120)
    # Tobler's hiking function, W = v0 * exp(-k * |slope - peak|).  Omit all
    # three for the published pace; set any of them and every edge is re-timed
    # from its slope histogram and the circuit is re-optimised around the new
    # costs -- a different pace really is a different itinerary, not the same
    # one with different numbers on it.
    tobler_v0: float | None = Field(None, ge=3000, le=9000)
    tobler_k: float | None = Field(None, ge=2.0, le=6.0)
    tobler_peak: float | None = Field(None, ge=-0.20, le=0.05)


def _cache_key(req: SolveRequest) -> str:
    canon = json.dumps({
        'max_hours': req.max_hours,
        'max_resupply_days': req.max_resupply_days,
        'town_nights': req.town_nights,
        'hiked': sorted(req.hiked),
        'time_budget': req.time_budget,
        'tobler': [req.tobler_v0, req.tobler_k, req.tobler_peak],
    }, sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()


def _ndjson(obj: dict) -> bytes:
    return (json.dumps(obj) + '\n').encode()


async def _run_solver(req: SolveRequest, key: str):
    cmd = [sys.executable, SOLVER_PY,
           '--max-hours', str(req.max_hours),
           '--time-budget', str(req.time_budget),
           '--progress', '--json-out', '-']
    if req.max_resupply_days is not None:
        cmd += ['--max-resupply-days', str(req.max_resupply_days)]
    if req.town_nights:
        cmd += ['--town-nights']
    for flag, val in (('--tobler-v0', req.tobler_v0),
                      ('--tobler-k', req.tobler_k),
                      ('--tobler-peak', req.tobler_peak)):
        if val is not None:
            if PROFILES is None:
                yield _ndjson({'type': 'error', 'message':
                               'custom Tobler parameters need segment_profiles.json; '
                               'generate it with python -m elevation.build'})
                return
            cmd += [flag, str(val)]
    hiked_file = None
    if req.hiked:
        hiked_file = os.path.join(WORKDIR, f'hiked_{key[:12]}.txt')
        with open(hiked_file, 'w', encoding='utf-8') as hf:
            hf.write('\n'.join(req.hiked))
        cmd += ['--hiked-csv', hiked_file]

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=WORKDIR,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        # The RESULT_JSON line is one ~1 MB stdout line; asyncio's default
        # readline limit is 64 KB and raises past it.
        limit=16 * 1024 * 1024)
    result = None
    tail: list[str] = []          # last output lines, for error reporting
    try:
        async with asyncio.timeout(req.time_budget + 60):
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode('utf-8', errors='replace').rstrip()
                tail = (tail + [line])[-5:]
                if line.startswith('PROGRESS '):
                    _, pct, label = line.split(' ', 2)
                    yield _ndjson({'type': 'progress', 'pct': int(pct),
                                   'label': label})
                elif line.startswith('RESULT_JSON '):
                    result = json.loads(line[len('RESULT_JSON '):])
            await proc.wait()
    except TimeoutError:
        proc.kill()
        yield _ndjson({'type': 'error', 'message': 'solver timed out'})
        return
    except Exception as exc:                      # never end a stream silently
        proc.kill()
        yield _ndjson({'type': 'error', 'message': f'{type(exc).__name__}: {exc}'})
        return
    finally:
        if hiked_file and os.path.exists(hiked_file):
            os.remove(hiked_file)

    if result is None:
        yield _ndjson({'type': 'error',
                       'message': f'solver exited {proc.returncode} '
                                  f'without a result: {" | ".join(tail)}'})
        return
    _cache[key] = result
    yield _ndjson({'type': 'result', **result})


@app.get('/health')
async def health():
    return {'ok': True, 'cached': len(_cache), 'queued': _waiting}


@app.post('/solve')
async def solve(req: SolveRequest, request: Request):
    global _waiting
    key = _cache_key(req)

    # A cache hit costs nothing, so it is served before either limit is
    # consulted: repeating a solve someone already paid for is not abuse, and
    # counting it would punish exactly the behaviour the cache rewards.
    if key in _cache:
        async def cached():
            yield _ndjson({'type': 'progress', 'pct': 100, 'label': 'Cached'})
            yield _ndjson({'type': 'result', **_cache[key]})
        return StreamingResponse(cached(), media_type='application/x-ndjson')

    if _waiting >= MAX_QUEUE_DEPTH:
        return JSONResponse(status_code=503, headers={'Retry-After': '60'},
                            content={'type': 'error', 'message':
                                     'The solver is busy — several itineraries are '
                                     'already being built. Try again in a minute.'})

    wait = _rate_limited(_client_ip(request))
    if wait:
        return JSONResponse(status_code=429,
                            headers={'Retry-After': str(int(wait) + 1)},
                            content={'type': 'error', 'message':
                                     f'Too many itineraries built from here. '
                                     f'Try again in {int(wait / 60) + 1} minute(s).'})

    async def stream():
        global _waiting
        _waiting += 1
        try:
            async with _solve_lock:
                # Another request may have populated the cache while we queued.
                if key in _cache:
                    yield _ndjson({'type': 'progress', 'pct': 100, 'label': 'Cached'})
                    yield _ndjson({'type': 'result', **_cache[key]})
                    return
                async for chunk in _run_solver(req, key):
                    yield chunk
        finally:
            _waiting -= 1

    return StreamingResponse(stream(), media_type='application/x-ndjson')
