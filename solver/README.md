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
6. **Multi-day splitting** — constrained dynamic program (state: position, last overnight, consecutive nights, days since resupply) that respects max/min daily hours and overnight rules, with a greedy + detour fallback.
7. **Output & reporting** — day-by-day itinerary text/JSON with constraint verification (no short days, no consecutive-overnight violations, full required-trail coverage).

### Key constraints

- Configurable max/min hiking hours per day (last day exempt from the minimum).
- Overnights only at legal locations (`BC`/`SH`/`CG` nodes); shelters and BC113 allow 1 consecutive night, other backcountry sites up to 3.
- Either direction of traversal satisfies coverage of a required edge; any edge may be repeated (deadheaded), but repeat time counts against the daily budget.
- Optional resupply-interval constraint (`MAX_DAYS_BETWEEN_RESUPPLY`).

## Files

| File | Description |
|------|-------------|
| `smokies_circuit_solver_20260509a.py` | The complete solver (all 7 pipeline steps). |
| `smokies_edge_list_20260509a.csv` | 468-row edge list with asymmetric hiking-time costs, node classifications, and closure flags. |
| `visualize.py` | Renders a solver JSON itinerary as an interactive Leaflet map (the same rendering now lives in `../docs/js/viz.js`). |
| `smokies_itinerary_12h_10h.txt` | Example output: open circuit, 12 h max / 10 h min days. |

## Running

Requires Python 3.10+ with `pandas`, `networkx`, and `ortools`:

```
pip install pandas networkx ortools
python smokies_circuit_solver_20260509a.py --max-hours 12 --min-hours 10
```

The solver writes a day-by-day itinerary as text and JSON. The JSON presets consumed by the [web app](../docs/) were generated this way for each combination of circuit type (open/closed), max day (10/12/14 h), and min day (8/10 h).

## Results so far

With 12 h max / 10 h min days, CP-SAT finds an optimal direction assignment in ~2–3 s with ≈84 hours of total deadhead, yielding a 53-day itinerary for both open and closed circuits. The remaining gap to the ~40-day theoretical lower bound is driven by campsite placement forcing short days, not by the routing itself.
