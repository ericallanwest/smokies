# GSMNP Circuit Solver

A solver that builds multi-day thru-hiking itineraries covering every official trail in Great Smoky Mountains National Park (the "900 Miler" map completion), formulated as a **Rural Windy Postman Problem with Hotel Selection (RWPP-HS)**:

- **Rural** — non-required edges (roads, connector paths) may be used to move efficiently between required trails.
- **Windy** — traversal cost is asymmetric: each direction of an edge has its own hiking time, estimated from elevation change via [Tobler's hiking function](https://en.wikipedia.org/wiki/Tobler%27s_hiking_function).
- **Hotel Selection** — each day must end at a legal overnight location (backcountry campsite, shelter, or developed campground), subject to consecutive-night limits.

The result is a single continuous walk (no driving between segments) that covers all required trails while minimizing the number of days.

## Pipeline

1. **Graph construction & node classification** — build a `networkx.MultiDiGraph` from the edge list. Node types by ID prefix: `TH` trailhead, `TI` trail intersection, `BC` backcountry campsite, `SH` shelter, `CG*` campground, `RI` road intersection. (MultiDiGraph is required because a few junction pairs are connected by two physically distinct required trails, e.g. Boogerman and Caldwell Fork.)
2. **All-pairs shortest paths** — Dijkstra over the full graph (required + optional edges), used for deadhead estimates throughout.
3. **Circuit analysis & trailhead selection** — evaluate closed-circuit (same start/end trailhead) and open-circuit (different trailheads) variants.
4. **Direction assignment** — choose a traversal direction for every required edge with **OR-Tools CP-SAT** (exact; solves to optimality in seconds), with a greedy + local-search fallback. Imbalances are then repaired with min-cost flow.
5. **Eulerian circuit construction** — Hierholzer's algorithm over the balanced arc set.
6. **Multi-day splitting** — constrained dynamic program (state: position, last overnight, consecutive nights, days since resupply) run in two phases: first with no daily floor to find the guaranteed-minimum day count, then a binary search for the largest daily floor that still admits a split at that count, so the shortest day is as long as possible without costing a day. Greedy + detour fallback when an overnight gap exceeds the daily budget.
7. **Output & reporting** — day-by-day itinerary text/JSON with constraint verification (no short days, no consecutive-overnight violations, full required-trail coverage).

### Key constraints

- Configurable max hiking hours per day. There is no user-facing minimum: the solver minimizes day count first, then maximizes the shortest day (last day exempt) at that count.
- Overnights only at legal locations. Self-supported: `BC`/`SH`/`CG` nodes plus the 10 resupply points; shelters and BC113 allow 1 consecutive night, other backcountry sites up to 3, town beds no cap. Supported: trailheads and frontcountry campgrounds only, no caps — the crew books the bed.
- Either direction of traversal satisfies coverage of a required edge; any edge may be repeated (deadheaded), but repeat time counts against the daily budget.
- Optional resupply window (`--max-resupply-days N`): at most N consecutive days without touching one of 10 resupply nodes (town-access trailheads and the two road campgrounds). Pass-through semantics — walking past a resupply point counts, since most are trailheads where overnighting is illegal. The Euler tour can go 100h+ between natural touches, so a per-stretch shortest-path plan splices minimum-cost out-and-back detours into the walk before day-splitting.
- Town nights are unconditional: the 10 resupply nodes are always legal overnight stops — a motel or hostel in the gateway town — with no consecutive-night cap (you can zero-day in town). This used to be `--town-nights`, off by default.
- Hiking style (`--style`): `supported` means a crew drives the hiker between a road each evening and a road each morning, so days need not chain and no resupply window applies. Deadhead the van can drive, road to road, is dropped from the walk rather than walked.

## Files

| File | Description |
|------|-------------|
| `smokies_circuit_solver_20260509a.py` | The complete solver (all 7 pipeline steps). |
| `smokies_edge_list_20260509a.csv` | 468-row edge list with asymmetric hiking-time costs, node classifications, and closure flags. |
| `visualize.py` | Renders a solver JSON itinerary as an interactive Leaflet map (the same rendering now lives in `../docs/js/viz.js`). |
| `smokies_itinerary_12h.txt` | Example output: open circuit, 12 h max days. |

## Running

Requires Python 3.10+ with `pandas`, `networkx`, and `ortools`:

```
pip install pandas networkx ortools
python smokies_circuit_solver_20260509a.py --max-hours 12
python smokies_circuit_solver_20260509a.py --max-hours 12 --max-resupply-days 5
python smokies_circuit_solver_20260509a.py --max-hours 14 --style supported
```

The solver writes a day-by-day itinerary as text and JSON. The JSON presets consumed by the [web app](../docs/) cover one configuration per reachable slider position:

| | day lengths | resupply windows | presets |
|---|---|---|---|
| `--style self-supported` | 8–16 h, 1 h steps | 4–8 days, or none | 54 |
| `--style supported` | 10–16 h (see below) | n/a | 7 |

Filenames are `preset_selfsup_<h>h[_r<N>].json` and `preset_supported_<h>h.json`; `tools/build_presets_index.py` writes `presets_index.json` beside them so the app looks a configuration up rather than rebuilding its name. Only the open walk is published — a closed circuit is asked for by naming the same start and finish, which is a live solve.

`--town-nights` is accepted and ignored: resupply points are always legal overnights now. That default used to be off, which published itineraries walking past a bed to reach a backcountry site; turning it on is worth a day at 12 h on its own (42 → 41 from the default start).

### Why supported starts at 10 h

A supported hiker is collected each night, so every day has to both begin and end somewhere the crew can reach. The binding constraint is whichever required trail is furthest from a pick-up point, and that is Lakeshore along Fontana Lake's north shore — 6.6 h from the nearest road at each end, making a road-only supported day 13.73 h at minimum.

The Fontana Lake boat shuttle to the Hazel Creek landing (TI051) attacks exactly that trail: minutes from the water where it is hours from tarmac. It drops the floor to **9.90 h**, which is why supported starts at 10 h. The published presets assume the boat; `--shuttle-nodes ""` gives road-only, and `--shuttle-nodes TI051,TH025` would add another landing. `tools/road_bound.py` recomputes both figures.

| Max day | Road only | With the boat |
|---------|-----------|---------------|
| 9 h | impossible | impossible |
| 10 h | impossible | 59 days |
| 11 h | impossible | 47 days |
| 12 h | impossible | 42 days |
| 13 h | impossible | 40 days |
| 14 h | 38 days | 37 days |
| 15 h | 35 days | 34 days |
| 16 h | 32 days | 31 days |

Supported is still not the faster option — at 16 h it needs 31 days against self-supported's 28, and walks ~40 h more, because exiting to a pick-up nightly costs more than skipping connectors saves. What it buys is a bed and a light pack.

## Results so far

CP-SAT finds an optimal direction assignment in ~2–3 s. Because some campsite gaps exceed a single day's budget, day splitting may require detour insertion; the solver always evaluates three candidates — the detour-free DP split (when one exists), a campsite chain plan, and a sweep of the greedy detour heuristic's floor parameter (each candidate refined by the exact DP) — and keeps the fewest-day result. Candidates that tie on day count are each balanced (largest feasible daily floor at that count) and the itinerary with the longest shortest day wins:

| Max day | Open circuit | Closed circuit | Shortest non-last day (open) |
|---------|--------------|----------------|------------------------------|
| 8 h | 65 days | 66 days | 4h49m |
| 10 h | 51 days | 49 days | 7h25m |
| 12 h | 41 days | 43 days | 9h06m |
| 14 h | 33 days | 34 days | 11h22m |
| 16 h | 30 days | 29 days | 13h34m |

The remaining gap to the theoretical lower bound is driven by campsite placement, not by the routing itself.

The 8 h case needs one extra trick: Cove Mountain (TH106–TI086, 8.4 mi, no intermediate junction or campsite) costs at least 9.4 h camp-to-camp, so it cannot fit in *any* interior day. The solver detects such arcs and rotates the circuit so the walk starts (closed) or ends (open) with them — day 1 and the final day run terminus-to-camp rather than camp-to-camp, which is the only rescue for an atomic arc. A livelock guard also protects the greedy detour heuristic, which previously could spin forever in campsite-sparse regions at tight budgets.

Resupply windows are nearly free at 12 h. The detour plan touches town-access points with tiny out-and-backs (6 min to Bryson City, 20 min to Cades Cove); the one costly case is the remote southwest corner, which forces an 8.4 h round trip to Fontana on the closed circuit:

| Window (12 h days) | Open circuit | Closed circuit | Extra repeat walking (open / closed) |
|--------------------|--------------|----------------|--------------------------------------|
| unlimited | 41 days | 43 days | — |
| ≤ 8 days | 41 days | 43 days | +0.0h / +1.9h |
| ≤ 7 days | 41 days | 43 days | +0.1h / +6.9h |
| ≤ 6 days | 41 days | 43 days | +1.6h / +7.8h |
| ≤ 5 days | 41 days | 43 days | +2.5h / +11.3h |
| ≤ 4 days | 42 days | 43 days | +8.6h / +16.3h |

Town nights turn the 10 resupply points into cap-free overnight options, which mostly helps where legal campsites are sparse near the park boundary — including Cove Mountain, which becomes interior-feasible at 8 h without the terminus rotation. This is now always on; the "standard" column is what the old default produced:

| Max day | Open (town / standard) | Closed (town / standard) |
|---------|------------------------|--------------------------|
| 8 h | **63** / 65 | **65** / 66 |
| 10 h | **49** / 51 | **47** / 49 |
| 12 h | **40** / 41 | **41** / 43 |
| 14 h | 33 / 33 | 34 / 34 |
| 16 h | 30 / 30 | **28** / 29 |
