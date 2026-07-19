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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

HERE = os.path.dirname(os.path.abspath(__file__))
SOLVER_DIR = os.environ.get('SOLVER_DIR', os.path.join(HERE, '..', 'solver'))
SOLVER_PY = os.path.join(SOLVER_DIR, 'smokies_circuit_solver_20260509a.py')
EDGE_CSV = os.path.join(SOLVER_DIR, 'smokies_edge_list_20260509a.csv')

# Scratch working directory: the solver reads its edge list from cwd and
# writes itinerary/preset files there, so give it a private sandbox.
WORKDIR = tempfile.mkdtemp(prefix='smokies_solver_')
shutil.copy(EDGE_CSV, WORKDIR)

# One solve at a time: the solver is CPU-bound and a Cloud Run instance has
# one core by default. Concurrent requests queue here (each solve is ~5-15 s).
_solve_lock = asyncio.Lock()
_cache: dict[str, dict] = {}

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


def _cache_key(req: SolveRequest) -> str:
    canon = json.dumps({
        'max_hours': req.max_hours,
        'max_resupply_days': req.max_resupply_days,
        'town_nights': req.town_nights,
        'hiked': sorted(req.hiked),
        'time_budget': req.time_budget,
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
    return {'ok': True, 'cached': len(_cache)}


@app.post('/solve')
async def solve(req: SolveRequest):
    key = _cache_key(req)

    async def stream():
        if key in _cache:
            yield _ndjson({'type': 'progress', 'pct': 100, 'label': 'Cached'})
            yield _ndjson({'type': 'result', **_cache[key]})
            return
        async with _solve_lock:
            # Another request may have populated the cache while we queued.
            if key in _cache:
                yield _ndjson({'type': 'progress', 'pct': 100, 'label': 'Cached'})
                yield _ndjson({'type': 'result', **_cache[key]})
                return
            async for chunk in _run_solver(req, key):
                yield chunk

    return StreamingResponse(stream(), media_type='application/x-ndjson')
