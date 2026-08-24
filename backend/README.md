# Smokies solve service

On-demand itinerary solving for the Great Smokies Circuit Planner. Wraps the
circuit solver (`../solver/`) in a small FastAPI app that streams solver
progress to the browser and returns results in the same JSON schema as the
pre-solved presets in `docs/data/`.

## API

`POST /solve` — body:

```json
{
  "max_hours": 12,
  "max_resupply_days": null,
  "town_nights": false,
  "hiked": ["2.0", "8.1"],
  "time_budget": 45,
  "tobler_v0": null,
  "tobler_k": null,
  "tobler_peak": null
}
```

### Hiking pace

`tobler_v0` (3000-9000 m/h), `tobler_k` (2.0-6.0) and `tobler_peak` (-0.20 to
0.05) override Tobler's hiking function, `W = v0 * exp(-k * |slope - peak|)`.
Leave all three null for the published pace; the presets in `docs/data/` are
exactly that solve.

Setting any of them re-times every edge from the slope histograms in
`segment_profiles.json` and **re-optimises the circuit around the new costs**.
This is deliberate. Changing the shape of the speed curve changes which
traversal directions are cheap, which changes the optimal circuit -- so a
different pace is a genuinely different itinerary, not the same one with
different numbers written on it. Expect the day count to move: at 12h/day, a
loaded backpacker (`k` 4.2) needs 46 days where the default needs 42, and a
fit hiker (`v0` 6600, `k` 3.0) needs 33.

The service refuses the request if `segment_profiles.json` is missing rather
than falling back to the baked costs, which would silently mix two pace models
in one solve. Regenerate it with `python -m elevation.build`.

`hiked` is the 900-Miler input: edge IDs (from the edge list / preset
`edge_id` values) already traversed; they become non-required.

The response streams NDJSON lines:

```
{"type": "progress", "pct": 45, "label": "Multi-day splitting"}
...
{"type": "result", "params": {...}, "best_found": false, "open": {...}, "closed": {...}}
```

`open` / `closed` match the preset schema exactly (`n_days`, `days[].arcs`,
optional `resupply_plan`), so the app's preset renderer consumes them as-is.
On failure the final line is `{"type": "error", "message": "..."}` instead.
Results are cached in memory by parameter hash — repeats return instantly.

`GET /health` — liveness + cache size.

## Run locally

```
cd backend
conda run -n minispatial uvicorn app:app --port 8080
```

Then in the app, open the site with `?backend=http://localhost:8080` to show
the Custom Solve panel.

## Limits

`/solve` is an unauthenticated CPU endpoint linked from a public page, so two
limits guard it. Both are per-instance and tunable without a rebuild
(`gcloud run services update smokies-solver --set-env-vars KEY=VALUE`).

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAX_QUEUE_DEPTH` | 4 | callers allowed to wait on the solve lock; beyond it, `503` immediately |
| `RATE_LIMIT_SOLVES` | 12 | solves per client per window |
| `RATE_LIMIT_WINDOW` | 600 | window in seconds |

The queue cap matters more than the per-client cap. Solves are serialised and
take 15-25 s, so without a depth limit the tenth caller waits three minutes and
times out anyway, having queued behind nine solves that already finished.
Refusing at once is cheaper and more honest, and the response carries
`Retry-After`.

**Cache hits bypass both.** Repeating a solve someone already paid for costs
nothing, and charging for it would punish exactly the behaviour the cache
rewards. Only work that reaches the solver counts.

`GET /health` reports `{"ok", "cached", "queued"}`.

## Deploy to Cloud Run

One-time setup: install the gcloud CLI, `gcloud auth login`, create/pick a
project, and create an Artifact Registry docker repo named `smokies`
(`gcloud artifacts repositories create smokies --repository-format=docker
--location=us-east1`).

From the repo root:

```
gcloud builds submit --config backend/cloudbuild.yaml .
gcloud run deploy smokies-solver \
  --image us-east1-docker.pkg.dev/$PROJECT_ID/smokies/solver:latest \
  --region us-east1 --allow-unauthenticated \
  --memory 1Gi --cpu 1 --min-instances 0 --max-instances 2 \
  --concurrency 2 --timeout 180
```

Scale-to-zero keeps idle cost ≈ $0; the first request after idle pays a
5–10 s cold start (within the 60 s solve budget). Point the app at the
printed service URL via `?backend=https://...run.app`.

Notes:
- `--concurrency 2` is deliberate.  The Cloud Run default is 80, which fits
  an I/O-bound web app whose requests mostly sit waiting on a database; this
  service spends its entire request pegging one vCPU inside CP-SAT.  At 80,
  eighty solves would share one core, each running ~80x slower, and all of
  them would blow the 180 s timeout together.  The subtler damage is worse
  than the timeouts: the CP-SAT budget is half the time remaining, so a
  starved solve returns a *worse circuit* rather than an error — the same
  silent degradation that once had the service answer 41 days where the
  published preset says 42.  At 2, Cloud Run scales out instead of
  oversubscribing, and a cheap `/health` request can still slip in alongside
  a running solve rather than queueing behind it.
  Measured on revision 00004: three concurrent solves (10h/14h/16h) all came
  back OPTIMAL after 11.7–14.5 s of CP-SAT and matched their presets — the
  same band as a solve running alone.
- Capacity is therefore 4 concurrent solves (2 instances x 2 each); beyond
  that Cloud Run queues.  Note that `MAX_QUEUE_DEPTH` cannot trip at this
  concurrency — at most one caller per instance is ever waiting on the solve
  lock — so it is now a backstop should `--concurrency` ever be raised, and
  the per-IP `RATE_LIMIT_SOLVES` window is the limit doing live work.
- The container is stateless; the in-memory result cache resets on cold
  start, which is fine (solves are ~5–15 s).
- CORS allows `ericallanwest.github.io` and localhost only (see `app.py`).
- No auth: the service is compute-only.  `--max-instances 2` caps the bill
  and the per-IP window caps abuse; add an API key check in `app.py` if that
  stops being enough.

## Build hygiene

Three settings keep build cost flat instead of climbing. None is expensive
today -- the whole project's storage runs about eight cents a month -- but all
three grow without bound if left alone.

- `.gcloudignore` keeps rasters, archives, the QGIS project and the published
  presets out of the uploaded tarball.  Every `builds submit` used to ship
  ~78 MB that no build step opens, and one submit spent ten minutes uploading.
  Now 15 MB.  It is a deny-list, not an allow-list: excluding a file a build
  needs would break the build, so only demonstrably unused things are listed.

- A lifecycle rule on `gs://<project>_cloudbuild` deletes `source/` objects
  after 30 days.  Each build leaves its tarball there permanently otherwise;
  nine builds had accumulated 699 MB.  Build outputs under `presets/` and
  `sweeps/` are left alone -- they are small and worth keeping.

- An Artifact Registry cleanup policy keeps the 5 most recent images and
  deletes untagged ones older than 30 days.  Deliberately conservative on both
  counts: Cloud Run revisions pin images by *digest*, so deleting one makes
  that revision un-rollbackable, and `untagged` means the policy can never
  remove an image something still points at by tag.  Five is one per existing
  revision.

Re-apply with:

    gcloud storage buckets update gs://$PROJECT_ID_cloudbuild       --lifecycle-file=lifecycle.json
    gcloud artifacts repositories set-cleanup-policies smokies       --location us-east1 --policy=cleanup.json --no-dry-run

Both accept `--dry-run` first; the registry one logs its intentions to Cloud
Logging rather than printing them.
