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
  "time_budget": 45
}
```

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
  --timeout 180
```

Scale-to-zero keeps idle cost ≈ $0; the first request after idle pays a
5–10 s cold start (within the 60 s solve budget). Point the app at the
printed service URL via `?backend=https://...run.app`.

Notes:
- The container is stateless; the in-memory result cache resets on cold
  start, which is fine (solves are ~5–15 s).
- CORS allows `ericallanwest.github.io` and localhost only (see `app.py`).
- No auth: the service is compute-only and rate-limited by
  `--max-instances`; add an API key check in `app.py` if abuse ever shows up.
