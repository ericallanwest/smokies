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
- Overnights only at legal locations (`BC`/`SH`/`CG` nodes); shelters and BC113 allow 1 consecutive night, other backcountry sites up to 3.
- Either direction of traversal satisfies coverage of a required edge; any edge may be repeated (deadheaded), but repeat time counts against the daily budget.
- Optional resupply-interval constraint (`MAX_DAYS_BETWEEN_RESUPPLY`).

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
```

The solver writes a day-by-day itinerary as text and JSON. The JSON presets consumed by the [web app](../docs/) were generated this way for each combination of circuit type (open/closed) and max day (10/12/14 h).

## Results so far

CP-SAT finds an optimal direction assignment in ~2–3 s. Because some campsite gaps exceed a single day's budget, day splitting requires detour insertion; the solver sweeps the greedy heuristic's floor parameter internally (each candidate refined by the exact DP) and keeps the fewest-day result. Floors that tie on day count produce different detour placements, so each tied candidate is balanced (largest feasible daily floor at that count) and the itinerary with the longest shortest day wins:

| Max day | Open circuit | Closed circuit | Shortest non-last day (open) |
|---------|--------------|----------------|------------------------------|
| 10 h | 51 days | 49 days | 7h25m |
| 12 h | 41 days | 43 days | 9h06m |
| 14 h | 33 days | 34 days | 11h22m |

The remaining gap to the theoretical lower bound is driven by campsite placement, not by the routing itself.
