import argparse
import os
import re
import time
import pandas as pd
import networkx as nx
from collections import Counter

_ap = argparse.ArgumentParser(description='GSMNP circuit solver')
_ap.add_argument('--max-hours', type=float, default=12.0, help='Max hiking hours per day (default: 12)')
_ap.add_argument('--max-resupply-days', type=int, default=None,
                 help='Max days between resupply visits (default: disabled)')
_ap.add_argument('--town-nights', action='store_true',
                 help='Allow overnights (motel/hostel) at resupply points')
_ap.add_argument('--hiked', type=str, default=None,
                 help='Comma-separated edge IDs already hiked (900-Miler input); '
                      'they become non-required but stay usable as connectors')
_ap.add_argument('--hiked-csv', type=str, default=None,
                 help='File of already-hiked edge IDs (one per line; commas ok)')
_ap.add_argument('--time-budget', type=float, default=None,
                 help='Soft wall-clock budget in seconds: heuristic sweeps stop '
                      'early and the best solution found so far is returned')
_ap.add_argument('--tobler-v0', type=float, default=None,
                 help='Tobler peak speed in metres/hour (default: 6000). Setting '
                      'any --tobler-* option recomputes every edge cost from the '
                      'slope histograms in segment_profiles.json instead of using '
                      'the baked cost columns')
_ap.add_argument('--tobler-k', type=float, default=None,
                 help='Tobler slope decay (default: 3.5). Higher punishes grade more')
_ap.add_argument('--tobler-peak', type=float, default=None,
                 help='Slope of peak speed as rise/run (default: -0.05, slightly downhill)')
_ap.add_argument('--profiles', type=str, default=None,
                 help='Path to segment_profiles.json (default: searched next to the '
                      'edge list, then under docs/data/)')
_ap.add_argument('--start-node', type=str, default=None,
                 help='Begin the itinerary at this trailhead or campground (e.g. TH252). Default: the highest-degree trailhead, which minimises repositioning.')
_ap.add_argument('--balance-alpha', type=float, default=0.90,
                 help='Among itineraries tied on days and resupply stops, reject any whose shortest non-last day falls below this fraction of the best tied candidate, then take the least total walking (default: 0.90)')
_ap.add_argument('--progress', action='store_true',
                 help='Emit machine-readable "PROGRESS <pct> <label>" lines on stdout')
_ap.add_argument('--json-out', type=str, default=None,
                 help="Write the solution JSON to PATH, or '-' to print it as a "
                      "single 'RESULT_JSON {...}' line on stdout")
_args = _ap.parse_args()

# ---------------------------------------------------------------------------
# Service plumbing: wall-clock budget, progress stream, JSON result output.
# These exist so a web backend can run this script as a subprocess, stream
# PROGRESS lines to a browser progress bar, and parse one RESULT_JSON line.
# ---------------------------------------------------------------------------
_T0 = time.time()
TIME_BUDGET = _args.time_budget
BUDGET_TRUNCATED = False   # set True wherever the budget cuts a sweep short
CPSAT_STATUS = None        # direction-assignment status, reported in the envelope
CPSAT_ELAPSED = None


def time_left() -> float:
    """Seconds remaining in the wall-clock budget (inf when unlimited)."""
    if TIME_BUDGET is None:
        return float('inf')
    return TIME_BUDGET - (time.time() - _T0)


def progress(pct, label: str) -> None:
    if _args.progress:
        print(f"PROGRESS {int(round(pct))} {label}", flush=True)


def emit_json(payload: dict) -> None:
    """Write the result per --json-out: pretty JSON to a file path, or a single
    compact 'RESULT_JSON {...}' stdout line (trivial for a wrapper to parse)."""
    if not _args.json_out:
        return
    import json
    if _args.json_out == '-':
        print("RESULT_JSON " + json.dumps(payload, separators=(',', ':')), flush=True)
    else:
        with open(_args.json_out, 'w', encoding='utf-8') as _jf:
            json.dump(payload, _jf, indent=2)
        print(f"Result JSON written to: {_args.json_out}")


def envelope_base() -> dict:
    """Metadata shared by every JSON result (success, complete-map, no-split)."""
    return {
        "params": {
            "max_hours": _args.max_hours,
            "tobler": {"v0_m_per_h": TOBLER_PARAMS[0], "k": TOBLER_PARAMS[1],
                       "peak_slope": TOBLER_PARAMS[2], "custom": TOBLER_CUSTOM},
            "max_resupply_days": _args.max_resupply_days,
            "town_nights": _args.town_nights,
            # What the hiker asked for, and where the walk actually begins --
            # they differ when nothing was pinned.
            "start_node_requested": _args.start_node,
            "start_node": globals().get('start_node'),
            "time_budget": TIME_BUDGET,
            # globals().get: a complete-map or no-split run emits an envelope
            # before step 4 has set these.
            "cpsat": {"status": CPSAT_STATUS, "seconds": CPSAT_ELAPSED,
                      "limit_seconds": globals().get('_cpsat_limit')},
        },
        "hiked_count": len(hiked_ids),
        "solve_seconds": round(time.time() - _T0, 2),
        "best_found": BUDGET_TRUNCATED,
    }

# ---------------------------------------------------------------------------
# Step 1 -- Graph construction & node classification
# ---------------------------------------------------------------------------

CSV_PATH = 'smokies_edge_list_20260509a.csv'
MAX_DAY_SECONDS = int(_args.max_hours * 3600)
# Env override exists so a batch can sweep the knob without editing code.
BALANCE_ALPHA = float(os.environ.get('SMOKIES_BALANCE_ALPHA', _args.balance_alpha))
# There is no user-facing minimum-day parameter.  The day split first finds
# the minimum possible day count with no floor, then maximizes the shortest
# day at that count (see split_days_balanced).
CPSAT_SEED = 20260509        # any fixed value; changing it reshuffles ties
BC_SINGLE_NIGHT_IDS = {'BC113'}  # former shelter -- 1 consecutive night cap like SH nodes

# Resupply configuration
# Nodes where the hiker can leave the trail network to resupply.
# The circuit start/end trailheads implicitly count as resupply points.
# Names mirror RESUPPLY_NODES in docs/js/viz.js -- keep in sync.
RESUPPLY_NODES = {
    'CGCAD': 'Cades Cove Campground',
    'TH264': 'Standing Bear Hostel (Davenport Gap)',
    'TH210': 'Cherokee',
    'TH158': 'Bryson City',
    'TH025': 'Fontana Village',
    'RI058': 'Townsend',
    'TH117': 'Gatlinburg',
    'TH119': 'Gatlinburg',
    'TH220': 'Cosby',
    'CGSMO': 'Smokemont Campground',
}
# Only these two resupply points are inside the park boundary; the rest
# require leaving the park, adding access miles/hours the graph does not
# capture.  Hence: as few resupply stops as possible (gaps as close to the
# max-days setting as the walk allows), and in-park points win cost ties.
IN_PARK_RESUPPLY = {'CGCAD', 'CGSMO'}
MAX_DAYS_BETWEEN_RESUPPLY = _args.max_resupply_days  # None = disabled

# Town nights: resupply points double as legal overnights (motel/hostel bed).
# Town beds have no consecutive-night cap, unlike shelters (1) and BC sites (3).
TOWN_NIGHTS = _args.town_nights
town_overnights: set = set(RESUPPLY_NODES) if TOWN_NIGHTS else set()

progress(2, "Loading edge list")
df = pd.read_csv(CSV_PATH)

# --- Custom hiking pace: re-time every edge from its slope histogram ------
# The cost columns in the CSV are Tobler at the default parameters.  A hiker
# with a different pace needs different numbers, and re-timing is not enough
# on its own: changing the shape of the speed curve changes which traversal
# directions are cheap, which changes the optimal circuit.  So the parameters
# enter here, before the graph exists, and the whole solve proceeds on them.
#
# segment_profiles.json carries, per segment, the distance and rise falling in
# each 1% slope bin.  Time is the sum over bins of distance / speed(slope), and
# the reverse direction is the same histogram with every slope negated -- so
# one histogram re-times both directions and they cannot disagree.
TOBLER_DEFAULTS = (6000.0, 3.5, -0.05)
_tob = (_args.tobler_v0, _args.tobler_k, _args.tobler_peak)
TOBLER_PARAMS = tuple(d if v is None else v for v, d in zip(_tob, TOBLER_DEFAULTS))
TOBLER_CUSTOM = any(v is not None for v in _tob)

def _find_profiles() -> str | None:
    if _args.profiles:
        return _args.profiles if os.path.exists(_args.profiles) else None
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(os.getcwd(), 'segment_profiles.json'),
                 os.path.join(here, 'segment_profiles.json'),
                 os.path.join(here, 'docs', 'data', 'segment_profiles.json'),
                 os.path.join(here, '..', 'docs', 'data', 'segment_profiles.json')):
        if os.path.exists(cand):
            return cand
    return None

if TOBLER_CUSTOM:
    import json as _json
    import math as _math
    _pp = _find_profiles()
    if _pp is None:
        raise SystemExit("--tobler-* needs segment_profiles.json; pass --profiles PATH "
                         "(generate it with: python -m elevation.build)")
    _prof = _json.load(open(_pp, encoding='utf-8'))['segments']
    _v0, _k, _peak = TOBLER_PARAMS

    def _speed(s: float) -> float:
        return _v0 * _math.exp(-_k * abs(s - _peak))

    def _time(bins: dict, reverse: bool) -> float:
        t = 0.0
        for _b, (dist_m, rise_m) in bins.items():
            if dist_m <= 0:
                continue
            s = rise_m / dist_m
            t += dist_m / _speed(-s if reverse else s) * 3600.0
        return t

    _n_retimed, _missing = 0, []
    for _i, _row in df.iterrows():
        _rec = _prof.get(str(float(_row['ID'])))
        if _rec is None:
            _missing.append(str(_row['ID']))
            continue
        df.at[_i, 'cost_A_to_B'] = int(round(_time(_rec['bins'], False)))
        df.at[_i, 'cost_B_to_A'] = int(round(_time(_rec['bins'], True)))
        _n_retimed += 1
    print(f"Tobler parameters: v0={_v0:.0f} m/h, k={_k:.2f}, peak={_peak:+.3f} "
          f"(default {TOBLER_DEFAULTS[0]:.0f}/{TOBLER_DEFAULTS[1]}/{TOBLER_DEFAULTS[2]})")
    print(f"  re-timed {_n_retimed} edge(s) from {os.path.relpath(_pp)}")
    if _missing:
        # Keeping the baked cost for an edge with no histogram would silently
        # mix two pace models in one solve; refuse instead.
        raise SystemExit(f"segment_profiles.json is missing {len(_missing)} edge(s): "
                         f"{', '.join(_missing[:5])}")

# --- 900-Miler input: already-hiked edges become non-required -------------
# The edges stay in the graph as connectors (usable for repositioning);
# they just no longer have to be covered.  Everything downstream keys off
# is_required, so this one flip is the entire partial-completion feature.
hiked_ids: set[str] = set()
if _args.hiked:
    hiked_ids |= {t.strip() for t in _args.hiked.split(',') if t.strip()}
if _args.hiked_csv:
    with open(_args.hiked_csv, encoding='utf-8-sig') as _hf:
        for _ln in _hf:
            _ln = _ln.split('#', 1)[0]
            hiked_ids |= {t for t in re.split(r'[,\s]+', _ln) if t}
if hiked_ids:
    # The CSV ID column is float64, so its canonical string form is "2.0" /
    # "8.1" (the form the preset JSONs also use).  Normalize bare-integer
    # input ("2") to match.
    def _canon_id(tok: str) -> str:
        try:
            return str(float(tok))
        except ValueError:
            return tok
    hiked_ids = {_canon_id(t) for t in hiked_ids}
    _all_ids = set(df['ID'].astype(str))
    _unknown = sorted(hiked_ids - _all_ids)
    if _unknown:
        raise SystemExit(f"--hiked: unknown edge ID(s): {', '.join(_unknown)}")
    _mask = df['ID'].astype(str).isin(hiked_ids) & (df['is_required'] == 1)
    _hiked_miles = float(df.loc[_mask, 'Miles'].sum())
    df.loc[_mask, 'is_required'] = 0
    print(f"Already-hiked input: {len(hiked_ids)} edge ID(s) -> "
          f"{int(_mask.sum())} required edge(s) marked complete "
          f"({_hiked_miles:.1f} mi removed from required set)")

# --- Node classification by ID prefix ---
#   TH  = trailhead        (car-accessible; legal circuit start/end)
#   TI  = trail intersection
#   BC  = backcountry campsite  (legal overnight)
#   SH  = shelter               (legal overnight)
#   CG* = developed campground  (legal overnight)
#   RI  = road intersection

def node_type(node_id: str) -> str:
    prefix = node_id[:2]
    return {
        'TH': 'trailhead',
        'TI': 'intersection',
        'BC': 'backcountry_campsite',
        'SH': 'shelter',
        'RI': 'road_intersection',
    }.get(prefix, 'campground' if node_id.startswith('CG') else 'unknown')

def is_legal_overnight(node_id: str) -> bool:
    return (node_id.startswith(('BC', 'SH', 'CG'))
            or (TOWN_NIGHTS and node_id in RESUPPLY_NODES))

# First pass: collect campsite flags and overnight-closure flags with OR logic
# across all CSV rows.  A node's status can appear in either the node_A or
# node_B column depending on which edge it anchors; take the logical OR.
campsite_flags: dict[str, bool] = {}
overnight_closed_flags: dict[str, bool] = {}
for _, row in df.iterrows():
    u, v = row['node_A'], row['node_B']
    campsite_flags[u] = campsite_flags.get(u, False) or bool(row['node_A_is_campsite'])
    campsite_flags[v] = campsite_flags.get(v, False) or bool(row['node_B_is_campsite'])
    overnight_closed_flags[u] = (overnight_closed_flags.get(u, False)
                                  or bool(row.get('node_A_overnight_closed', 0)))
    overnight_closed_flags[v] = (overnight_closed_flags.get(v, False)
                                  or bool(row.get('node_B_overnight_closed', 0)))

# Build the directed multigraph.
# MultiDiGraph is required because three pairs of trail junctions are shared
# by two physically distinct required trails (e.g. Boogerman and Caldwell Fork
# both run between TI272 and TI275).  DiGraph would silently overwrite one arc;
# MultiDiGraph stores both using the CSV row ID as the edge key.
G = nx.MultiDiGraph()

trail_closed_ids: list[str] = []   # track which edges are removed due to closure

for _, row in df.iterrows():
    u, v = row['node_A'], row['node_B']
    row_id = str(row['ID'])

    # Add/update nodes -- G.add_node is idempotent; later calls overwrite attrs.
    # Nodes are always added even if their edge is closed, so the graph retains
    # the full node set for diagnostics.
    for node in (u, v):
        G.add_node(
            node,
            node_type=node_type(node),
            is_campsite=campsite_flags.get(node, False),
            is_legal_overnight=is_legal_overnight(node),
            is_overnight_closed=overnight_closed_flags.get(node, False),
            is_resupply=(node in RESUPPLY_NODES),
        )

    # Trail closure: edge is physically impassable -- skip it entirely.
    is_trail_closed = bool(row.get('is_trail_closed', 0))
    if is_trail_closed:
        trail_closed_ids.append(row_id)
        continue

    shared = dict(
        edge_id=row_id,
        trail=row['Trail'],
        segment=row['Endpoints'],
        miles=float(row['Miles']),
        required=bool(row['is_required']),
    )

    # Forward arc A -> B  (key keeps this arc distinct from any parallel arc)
    G.add_edge(u, v, key=f"{row_id}_fwd", **shared,
               weight=int(row['cost_A_to_B']),
               elev_start=int(row['elev_A']),
               elev_end=int(row['elev_B']),
               gain=int(row['gain_A_to_B']))

    # Reverse arc B -> A
    G.add_edge(v, u, key=f"{row_id}_rev", **shared,
               weight=int(row['cost_B_to_A']),
               elev_start=int(row['elev_B']),
               elev_end=int(row['elev_A']),
               gain=int(row['gain_B_to_A']))

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

n_nodes = G.number_of_nodes()
n_edges = G.number_of_edges()
n_required = sum(1 for *_, d in G.edges(data=True) if d['required'])
n_deadhead = n_edges - n_required

type_counts = Counter(d['node_type'] for _, d in G.nodes(data=True))
overnight_nodes = [n for n, d in G.nodes(data=True) if d['is_legal_overnight']]
trailhead_nodes = [n for n, d in G.nodes(data=True) if d['node_type'] == 'trailhead']
VALID_CIRCUIT_ENDPOINTS = {'trailhead', 'campground'}   # TH + CG only

print("=" * 60)
print("GRAPH SUMMARY")
print("=" * 60)
print(f"Nodes          : {n_nodes}")
print(f"Directed edges : {n_edges}  ({n_required} required, {n_deadhead} non-required)")
print()
print("Node types:")
for ntype, count in sorted(type_counts.items()):
    print(f"  {ntype:<24} {count}")
print(f"\nLegal overnight nodes : {len(overnight_nodes)}")
print(f"Trailhead nodes       : {len(trailhead_nodes)}")
progress(5, "Graph built")

# With every required edge already hiked there is nothing to solve.
if n_required == 0:
    print("\nAll required trails already hiked -- map complete! Nothing to solve.")
    _env = envelope_base()
    _env.update(map_complete=True, remaining_required_miles=0.0,
                open=None, closed=None)
    emit_json(_env)
    progress(100, "Map already complete")
    raise SystemExit(0)

# --- Trail closures ---
if trail_closed_ids:
    print(f"\nTrail closures (edges removed): {len(trail_closed_ids)}")
    for cid in trail_closed_ids:
        row = df[df['ID'].astype(str) == cid].iloc[0]
        print(f"  ID {cid}: {row['Trail']} ({row['Endpoints']})")

# --- Overnight closures ---
overnight_closed_nodes = [n for n, d in G.nodes(data=True)
                          if d.get('is_overnight_closed', False)
                          and d['is_legal_overnight']]
if overnight_closed_nodes:
    print(f"\nOvernight closures (cannot sleep here): {len(overnight_closed_nodes)}")
    for n in sorted(overnight_closed_nodes):
        print(f"  {n}  ({G.nodes[n]['node_type']})")

# --- Resupply nodes ---
resupply_in_graph = [n for n in G.nodes() if G.nodes[n].get('is_resupply', False)]
print(f"\nResupply nodes in graph : {len(resupply_in_graph)}")
if resupply_in_graph:
    for n in sorted(resupply_in_graph):
        print(f"  {n}  ({G.nodes[n]['node_type']})")
if MAX_DAYS_BETWEEN_RESUPPLY is not None:
    print(f"Max days between resupply : {MAX_DAYS_BETWEEN_RESUPPLY}")
else:
    print("Max days between resupply : disabled")
print(f"Town nights at resupply points : "
      f"{'enabled (no consecutive-night cap)' if TOWN_NIGHTS else 'disabled'}")

# --- Strong connectivity -- full graph ---
print()
print("=" * 60)
print("CONNECTIVITY")
print("=" * 60)

full_sc = nx.is_strongly_connected(G)
print(f"Full graph strongly connected : {full_sc}")

if not full_sc:
    sccs = sorted(nx.strongly_connected_components(G), key=len, reverse=True)
    print(f"  Number of SCCs : {len(sccs)}")
    for i, scc in enumerate(sccs):
        sample = ', '.join(sorted(scc)[:6])
        suffix = ', ...' if len(scc) > 6 else ''
        print(f"  SCC {i+1:2d} ({len(scc):3d} nodes): {sample}{suffix}")
    if trail_closed_ids:
        print(f"\n  WARNING: trail closures ({len(trail_closed_ids)} edge(s) removed) "
              "may have broken connectivity. The solver requires a strongly "
              "connected graph. Review closed segments.")

# --- Strong connectivity -- required-edge subgraph ---
req_edge_list = [(u, v, k) for u, v, k, d in G.edges(keys=True, data=True) if d['required']]
G_req = G.edge_subgraph(req_edge_list)
req_sc = nx.is_strongly_connected(G_req)
print(f"\nRequired-edge subgraph strongly connected : {req_sc}")

if not req_sc:
    req_sccs = sorted(nx.strongly_connected_components(G_req), key=len, reverse=True)
    print(f"  Number of SCCs in required subgraph : {len(req_sccs)}")
    for i, scc in enumerate(req_sccs):
        sample = ', '.join(sorted(scc)[:6])
        suffix = ', ...' if len(scc) > 6 else ''
        print(f"  SCC {i+1:2d} ({len(scc):3d} nodes): {sample}{suffix}")

# --- Edge collision audit ---
# NetworkX DiGraph silently overwrites attributes when add_edge() is called
# a second time for the same (u, v) pair.  This happens if a non-required
# connector row has (node_A, node_B) that matches the *reverse* of a
# required trail row, or if two rows share the same endpoint pair.
print()
print("=" * 60)
print("EDGE COLLISION AUDIT")
print("=" * 60)

csv_req_miles = df[df['is_required'] == 1]['Miles'].sum()
print(f"CSV direct sum (required rows)     : {csv_req_miles:.1f} mi")

# Check for duplicate directed pairs across all CSV rows
all_pairs = []
for _, row in df.iterrows():
    all_pairs.append((row['node_A'], row['node_B'], bool(row['is_required']), row['Miles']))
    all_pairs.append((row['node_B'], row['node_A'], bool(row['is_required']), row['Miles']))

from collections import defaultdict
pair_rows: dict = defaultdict(list)
for u, v, req, miles in all_pairs:
    pair_rows[(u, v)].append((req, miles))

collisions = {pair: rows for pair, rows in pair_rows.items() if len(rows) > 1}
req_clobbered = []
for (u, v), rows in collisions.items():
    # A collision is harmful if an earlier required arc is overwritten by a
    # non-required one (the last write wins in networkx add_edge).
    if rows[-1][0] is False and any(r[0] is True for r in rows[:-1]):
        req_clobbered.append((u, v, rows))

if req_clobbered:
    print(f"Required arcs clobbered by non-required overwrites: {len(req_clobbered)}")
    lost_miles = 0.0
    for u, v, rows in req_clobbered:
        for req, miles in rows[:-1]:
            if req:
                print(f"  {u} -> {v}  ({miles} mi required, overwritten)")
                lost_miles += miles
    print(f"  Total lost miles (each arc counted once): {lost_miles:.1f}")
else:
    print("No required arcs clobbered by non-required overwrites.")

# Also flag any duplicate pairs regardless of required flag
dup_pairs = {pair: rows for pair, rows in pair_rows.items() if len(rows) > 1}
print(f"Total directed pairs appearing more than once: {len(dup_pairs)}")

# --- Required-edge coverage summary ---
# Ground-truth miles come directly from the CSV, not the graph
total_req_miles = csv_req_miles

req_time_seconds = sum(
    d['weight'] for _, _, _, d in G.edges(keys=True, data=True) if d['required']
)
print()
print("=" * 60)
print("REQUIRED TRAIL COVERAGE")
print("=" * 60)
print(f"Unique required trail miles : {total_req_miles:.1f}")
print(f"Lower bound hiking time     : {req_time_seconds / 2 / 3600:.1f} h  "
      f"(cheapest direction per edge)")
print(f"  -> minimum days at {MAX_DAY_SECONDS/3600:.0f} h/day : "
      f"{req_time_seconds / 2 / MAX_DAY_SECONDS:.1f}")

# --- SCC interpretation ---
# The required subgraph has multiple SCCs because some trails are dead-end
# spurs where the only return path runs through non-required connector edges
# (roads, access roads, etc.).  Since the FULL graph is strongly connected,
# every required node IS reachable; the solver will just use non-required
# edges for some of the repositioning.  Nodes in small SCCs will necessarily
# incur deadhead time in both directions (hike in on required, hike out on
# non-required, or vice versa).
if not req_sc:
    print()
    print("Note: required-subgraph SCCs do not imply disconnected trips.")
    print("The full graph is strongly connected, so all nodes are reachable.")
    print("Nodes in small SCCs will need non-required edges for repositioning.")
    print()
    # Identify the dominant SCC and which nodes are outside it
    req_sccs = sorted(nx.strongly_connected_components(G_req), key=len, reverse=True)
    main_scc = req_sccs[0]
    outlier_nodes = set()
    for scc in req_sccs[1:]:
        outlier_nodes |= scc
    print(f"Nodes requiring non-required repositioning: {len(outlier_nodes)}")
    print("  " + ", ".join(sorted(outlier_nodes)))

# ---------------------------------------------------------------------------
# Step 2 -- All-Pairs Shortest Paths (APSP)
# ---------------------------------------------------------------------------
# D[u][v]  = minimum hiking seconds from u to v across any combination of arcs
# P[u][v]  = ordered list of node IDs on that shortest path
#
# For parallel arcs (MultiDiGraph), Dijkstra naturally picks the cheapest one.
# Non-required connector edges are included so the solver can route through
# roads and access paths when deadheading -- their time still counts against
# the daily budget.
# ---------------------------------------------------------------------------

print()
print("=" * 60)
print("STEP 2 -- ALL-PAIRS SHORTEST PATHS")
print("=" * 60)
progress(8, "All-pairs shortest paths")
print(f"Running Dijkstra from each of {G.number_of_nodes()} nodes ...")

D: dict[str, dict[str, float]] = {}
P: dict[str, dict[str, list[str]]] = {}

for source, (dists, paths) in nx.all_pairs_dijkstra(G, weight='weight'):
    D[source] = dists
    P[source] = paths

print("APSP complete.")


def edge_path(G: nx.MultiDiGraph,
              node_path: list[str]) -> list[tuple[str, str, str, dict]]:
    """Convert a node sequence to a list of (u, v, key, data) arc tuples.

    Between each consecutive node pair, selects the parallel arc with the
    lowest weight -- the same choice Dijkstra made when building the path.
    """
    result = []
    for u, v in zip(node_path[:-1], node_path[1:]):
        best_key = min(G[u][v], key=lambda k: G[u][v][k]['weight'])
        result.append((u, v, best_key, G[u][v][best_key]))
    return result


# --- Validation spot-checks ---
print()
print("Spot-check: direct arcs (D[u][v] must equal arc weight)")
checks = [
    # (u, v, expected_trail_name)
    ('TH015', 'TI004', 'Abrams Falls'),
    ('TH148', 'TI151', 'Alum Cave'),
    ('TI180', 'SH114', 'Boulevard'),
]
all_ok = True
for u, v, trail in checks:
    direct_weight = min(G[u][v][k]['weight'] for k in G[u][v])
    apsp_weight = D[u][v]
    match = "OK" if direct_weight == apsp_weight else "MISMATCH"
    if match == "MISMATCH":
        all_ok = False
    print(f"  {trail:<20} {u}->{v}  arc={direct_weight}s  APSP={apsp_weight}s  {match}")

# Multi-hop check: Newfound Gap Road trailhead to Appalachian Trail junction
# via the Alum Cave trail -- should be longer than the direct arc
u, v = 'TH148', 'SH114'   # Newfound Gap Road -> Mount LeConte Shelter
apsp_d = D[u][v]
hop_path = P[u][v]
print()
print(f"Multi-hop path {u} -> {v}:")
print(f"  {len(hop_path)-1} arcs, {apsp_d}s ({apsp_d/3600:.2f} h)")
print(f"  nodes: {' -> '.join(hop_path)}")

if not all_ok:
    print("\nWARNING: one or more spot-checks failed -- review edge weights.")

# --- Reachability check ---
unreachable = [(u, v) for u in D for v in G.nodes() if v not in D[u]]
print()
if unreachable:
    print(f"WARNING: {len(unreachable)} unreachable (u,v) pairs found!")
    for u, v in unreachable[:10]:
        print(f"  {u} -> {v}")
else:
    print("Reachability: all node pairs have finite shortest paths. OK")

# --- Summary stats relevant to hotel selection ---
overnight_nodes = [n for n, d in G.nodes(data=True) if d['is_legal_overnight']]

# For every node, find the nearest legal overnight node
nearest_overnight: dict[str, tuple[float, str]] = {}
for u in G.nodes():
    best = min(
        ((D[u].get(v, float('inf')), v) for v in overnight_nodes if v != u),
        key=lambda x: x[0],
    )
    nearest_overnight[u] = best  # (seconds, node_id)

max_dist, worst_node = max((d, n) for n, (d, _) in nearest_overnight.items())
median_dist = sorted(d for d, _ in nearest_overnight.values())[len(nearest_overnight) // 2]

print()
print("Overnight-node proximity (worst-case forced detour estimate):")
print(f"  Nearest overnight from any node -- median : {median_dist/3600:.2f} h")
print(f"  Nearest overnight from any node -- worst  : {max_dist/3600:.2f} h  "
      f"(from {worst_node} to {nearest_overnight[worst_node][1]})")

# Campsite-to-campsite distance matrix stats
cs_to_cs = [D[u][v] for u in overnight_nodes for v in overnight_nodes if u != v]
cs_to_cs_sorted = sorted(cs_to_cs)
n_cs = len(overnight_nodes)
print()
print(f"Campsite-to-campsite shortest paths ({n_cs} overnight nodes, "
      f"{n_cs*(n_cs-1):,} pairs):")
print(f"  Min    : {cs_to_cs_sorted[0]/3600:.2f} h")
print(f"  Median : {cs_to_cs_sorted[len(cs_to_cs_sorted)//2]/3600:.2f} h")
print(f"  Max    : {cs_to_cs_sorted[-1]/3600:.2f} h  (graph diameter among campsites)")
pairs_over_budget = sum(1 for d in cs_to_cs if d > MAX_DAY_SECONDS)
print(f"  Pairs > {MAX_DAY_SECONDS//3600}h budget : {pairs_over_budget} "
      f"({100*pairs_over_budget/len(cs_to_cs):.1f}%)  "
      f"[these pairs cannot share a single day]")

# ---------------------------------------------------------------------------
# Step 4 -- Direction Assignment + Eulerian Transformation
# ---------------------------------------------------------------------------
# Required edges are undirected (either direction counts as coverage).
# We must assign each required edge a traversal direction, then balance
# every node's in-degree == out-degree via min-cost deadhead arcs.
#
# Pipeline:
#   4A  Greedy direction assignment (pick cheaper direction per edge)
#   4B  Min-cost flow to balance node degrees
#   4C  Local search: flip directions that reduce total cost
#   4D  Build G_euler (chosen required arcs + deadhead arc-paths)
#   4E  Verify every node is degree-balanced
# ---------------------------------------------------------------------------
import time

print()
print("=" * 60)
print("STEP 4 -- DIRECTION ASSIGNMENT + EULERIAN TRANSFORMATION")
print("=" * 60)
progress(18, "Direction assignment (CP-SAT)")

if 'is_trail_closed' in df.columns:
    required_rows = df[(df['is_required'] == 1) & (df['is_trail_closed'] != 1)]
else:
    required_rows = df[df['is_required'] == 1]


def budget_forced_directions() -> dict[str, str]:
    """Pin required edges that only one direction can cover inside a day.

    An interior day runs camp -> ... -> camp, so covering an arc costs at least
    (nearest camp in) + (traversal) + (nearest camp out).  Past the daily budget
    the arc is coverable only on day 1 or the last day, and
    retarget_termini_for_budget can rotate the walk to rescue exactly one such
    arc -- a second one leaves the circuit with no legal split at all.

    Direction is often the whole difference.  At 8h, Roundtop needs 8.01h
    camp-to-camp as TH059->TH071 but 7.81h as TH071->TH059: it fails the budget
    by 36 seconds, and only because the cost objective preferred the traversal
    that is 478s cheaper in isolation.  Deciding that on total tour cost alone
    is what cost the 8h tier its itineraries.  So pin those edges before the
    objective sees them; edges that fit either way, or neither, stay free and
    the objective still chooses.
    """
    INF = float('inf')
    _out, _back = {}, {}
    def out_of(n):
        if n not in _out:
            _out[n] = min(D[n].get(c, INF) for c in overnight_nodes)
        return _out[n]
    def back_of(n):
        if n not in _back:
            _back[n] = min(D[c].get(n, INF) for c in overnight_nodes)
        return _back[n]

    forced: dict[str, str] = {}
    notes: list[str] = []
    for _, row in required_rows.iterrows():
        rid  = str(row['ID'])
        A, B = row['node_A'], row['node_B']
        fwd  = back_of(A) + int(row['cost_A_to_B']) + out_of(B)
        rev  = back_of(B) + int(row['cost_B_to_A']) + out_of(A)
        if fwd > MAX_DAY_SECONDS >= rev:
            forced[rid] = 'rev'
            notes.append(f"  {B}->{A}  {row['Trail']}  "
                         f"({rev / 3600:.2f}h camp-to-camp, vs {fwd / 3600:.2f}h reversed)")
        elif rev > MAX_DAY_SECONDS >= fwd:
            forced[rid] = 'fwd'
            notes.append(f"  {A}->{B}  {row['Trail']}  "
                         f"({fwd / 3600:.2f}h camp-to-camp, vs {rev / 3600:.2f}h reversed)")
    print()
    if forced:
        print(f"Budget-aware directions: {len(forced)} required edge(s) pinned to the "
              f"only direction that fits an interior day at "
              f"{MAX_DAY_SECONDS / 3600:.0f}h:")
        for n in notes:
            print(n)
    else:
        print(f"Budget-aware directions: every required edge fits an interior day at "
              f"{MAX_DAY_SECONDS / 3600:.0f}h in both directions -- nothing pinned")
    return forced


budget_forced = budget_forced_directions()

# chosen[row_id] = (from_node, to_node, arc_key)
# Each row's forward key is  "{row_id}_fwd", reverse is "{row_id}_rev".
chosen: dict[str, tuple[str, str, str]] = {}
for _, row in required_rows.iterrows():
    row_id = str(row['ID'])
    u, v = row['node_A'], row['node_B']
    _pin = budget_forced.get(row_id)
    if _pin == 'fwd' or (_pin is None
                         and int(row['cost_A_to_B']) <= int(row['cost_B_to_A'])):
        chosen[row_id] = (u, v, f"{row_id}_fwd")
    else:
        chosen[row_id] = (v, u, f"{row_id}_rev")


def arc_traversal_cost(chosen: dict) -> int:
    return sum(G[u][v][k]['weight'] for u, v, k in chosen.values())


def compute_imbalances(chosen: dict) -> dict[str, int]:
    """imbalance[v] = in_degree_required - out_degree_required."""
    imb: dict[str, int] = defaultdict(int)
    for u, v, _ in chosen.values():
        imb[u] -= 1  # outgoing arc leaves u
        imb[v] += 1  # incoming arc arrives at v
    return dict(imb)


def solve_min_cost_flow(imbalance: dict) -> tuple[dict, int]:
    """
    Transportation problem: route flow from excess nodes (imbalance > 0)
    to deficit nodes (imbalance < 0) using APSP costs.

    Returns (flow_dict, total_deadhead_cost).
    flow_dict[s][t] = integer number of deadhead paths from s to t.
    """
    supply = {v: s for v, s in imbalance.items() if s > 0}
    demand = {v: -s for v, s in imbalance.items() if s < 0}

    if not supply:
        return {}, 0

    H = nx.DiGraph()
    for v, s in supply.items():
        H.add_node(v, demand=-s)   # negative demand = supply in nx convention
    for v, d in demand.items():
        H.add_node(v, demand=d)    # positive demand = sink
    for s in supply:
        for t in demand:
            H.add_edge(s, t, weight=int(D[s][t]), capacity=1000)

    flow_dict = nx.min_cost_flow(H)
    total_cost = nx.cost_of_flow(H, flow_dict)
    return flow_dict, total_cost


def solve_step4_cpsat(G, required_rows, MAX_DH=30, time_limit=120,
                      forced_dirs=None):
    """
    Exact direction assignment via OR-Tools CP-SAT.

    Binary dir[e] vars (0=A→B, 1=B→A) + integer deadhead arc vars.
    Balance constraints enforce in-flow == out-flow at every node.
    Objective: minimize required traversal cost + deadhead arc cost.

    Returns (chosen, dh_arc_dict, objective_value) on success, or None.
    """
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        print("  ortools not available -- falling back to greedy+LS")
        return None

    print("\nCP-SAT direction assignment ...")
    t0 = time.time()

    model = cp_model.CpModel()

    required_endpoints = {}
    for _, row in required_rows.iterrows():
        row_id = str(row['ID'])
        required_endpoints[row_id] = (str(row['node_A']), str(row['node_B']))

    # dir_vars[row_id] = 0 → A→B,  1 → B→A
    dir_vars = {rid: model.NewBoolVar(f'd{rid}') for rid in required_endpoints}

    # Edges only one direction can cover inside a day are decided by the
    # budget, not by the cost objective (see budget_forced_directions).
    for rid, want in (forced_dirs or {}).items():
        if rid in dir_vars:
            model.Add(dir_vars[rid] == (0 if want == 'fwd' else 1))

    all_arcs = list(G.edges(keys=True))
    dh_vars  = {(u, v, k): model.NewIntVar(0, MAX_DH, f'dh_{i}')
                for i, (u, v, k) in enumerate(all_arcs)}

    # Balance constraints: in-flow == out-flow at every node
    for n in G.nodes():
        in_exprs  = []
        out_exprs = []

        for rid, (A, B) in required_endpoints.items():
            d = dir_vars[rid]
            if A == n:
                in_exprs.append(d)        # arrives when dir=1 (B→A)
                out_exprs.append(d.Not()) # departs when dir=0 (A→B)
            if B == n:
                in_exprs.append(d.Not()) # arrives when dir=0 (A→B)
                out_exprs.append(d)      # departs when dir=1 (B→A)

        for (u, v, k) in all_arcs:
            dh = dh_vars[(u, v, k)]
            if v == n:
                in_exprs.append(dh)
            if u == n:
                out_exprs.append(dh)

        if in_exprs or out_exprs:
            model.Add(
                cp_model.LinearExpr.Sum(in_exprs) ==
                cp_model.LinearExpr.Sum(out_exprs)
            )

    # Objective: minimize total traversal + deadhead cost (all integer seconds)
    obj_terms  = []
    obj_coeffs = []
    for rid, (A, B) in required_endpoints.items():
        d        = dir_vars[rid]
        cost_fwd = G[A][B][f"{rid}_fwd"]['weight']
        cost_rev = G[B][A][f"{rid}_rev"]['weight']
        obj_terms.extend([d.Not(), d])
        obj_coeffs.extend([cost_fwd, cost_rev])
    for (u, v, k), dh in dh_vars.items():
        obj_terms.append(dh)
        obj_coeffs.append(G[u][v][k]['weight'])
    model.Minimize(cp_model.LinearExpr.WeightedSum(obj_terms, obj_coeffs))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    # Reproducibility over speed.  CP-SAT's portfolio search races several
    # strategies across threads and returns whichever finishes first, so an
    # unchanged model can yield a different optimum on every run -- which
    # would mean a user nudging a Tobler slider to 3.6 and back to 3.5 gets a
    # different trip.  One worker and a fixed seed make the solve a function
    # of its inputs.  Both are needed: the seed alone does not tame the race.
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = CPSAT_SEED
    status = solver.Solve(model)

    elapsed = time.time() - t0
    global CPSAT_STATUS, CPSAT_ELAPSED, BUDGET_TRUNCATED
    CPSAT_STATUS = solver.StatusName(status)
    CPSAT_ELAPSED = round(elapsed, 1)
    print(f"  Status : {CPSAT_STATUS}  ({elapsed:.1f}s)")
    if status == cp_model.FEASIBLE:
        # Feasible-not-optimal means the limit cut the search off, so the
        # circuit is the best found rather than the best there is.  Callers
        # read best_found to decide whether to trust a comparison against
        # another solve, so it has to cover this stage too, not only the
        # floor sweep.
        BUDGET_TRUNCATED = True
        print("  CP-SAT hit its time limit -- direction assignment is not proven "
              "optimal; raise --time-budget for a reproducible circuit")

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        print("  No feasible solution found -- falling back to greedy+LS")
        return None

    new_chosen = {}
    for rid, (A, B) in required_endpoints.items():
        if solver.Value(dir_vars[rid]) == 0:
            new_chosen[rid] = (A, B, f"{rid}_fwd")
        else:
            new_chosen[rid] = (B, A, f"{rid}_rev")

    dh_arc_dict = {
        (u, v, k): solver.Value(dh_vars[(u, v, k)])
        for (u, v, k) in all_arcs
        if solver.Value(dh_vars[(u, v, k)]) > 0
    }

    return new_chosen, dh_arc_dict, solver.ObjectiveValue()


# ---------------------------------------------------------------------------
# 4A: Direction assignment — CP-SAT exact solver with greedy+LS fallback
# ---------------------------------------------------------------------------
# Under a wall-clock budget CP-SAT cannot have the full 120 s default, but a
# flat 10 s cap made the answer depend on how fast the machine is: direction
# assignment reaches OPTIMAL in 6-8 s on a workstation and can exceed 10 s on a
# smaller cloud vCPU, and a timed-out assignment is a different circuit.  The
# service and the published presets then disagreed at identical parameters --
# 42 days from the preset, 41 from the same request to the deployed solver.
# Half the remaining budget scales with the budget instead of the hardware.
_cpsat_limit = (120 if TIME_BUDGET is None
                else max(5, min(120, int(time_left() * 0.5))))
cpsat_result = solve_step4_cpsat(G, required_rows, MAX_DH=30,
                                 time_limit=_cpsat_limit,
                                 forced_dirs=budget_forced)
progress(38, "Direction assignment complete")

if cpsat_result is not None:
    chosen, dh_arc_dict, cpsat_obj = cpsat_result
    imbalance    = compute_imbalances(chosen)
    req_cost     = arc_traversal_cost(chosen)
    dh_cost_arc  = sum(G[u][v][k]['weight'] * cnt
                       for (u, v, k), cnt in dh_arc_dict.items())
    flow_dict    = None
    method_4     = "CP-SAT"
    total_cost   = req_cost + dh_cost_arc
    print()
    print("CP-SAT direction assignment:")
    print(f"  Required arc cost : {req_cost:>10,}s  ({req_cost/3600:.1f}h)")
    print(f"  Deadhead cost     : {dh_cost_arc:>10,}s  ({dh_cost_arc/3600:.1f}h)")
    print(f"  Total tour cost   : {total_cost:>10,}s  ({total_cost/3600:.1f}h)")
    print(f"  Lower bound days  : {total_cost/MAX_DAY_SECONDS:.1f}")
else:
    dh_arc_dict = None
    method_4    = "greedy+LS"

    # ---------------------------------------------------------------------------
    # 4B: Greedy direction assignment
    # ---------------------------------------------------------------------------
    imbalance = compute_imbalances(chosen)
    n_imbalanced = sum(1 for v in imbalance.values() if v != 0)
    total_supply = sum(s for s in imbalance.values() if s > 0)
    req_cost = arc_traversal_cost(chosen)
    flow_dict, deadhead_cost = solve_min_cost_flow(imbalance)
    best_total = req_cost + deadhead_cost

    print(f"\nGreedy direction assignment:")
    print(f"  Required arc cost : {req_cost:>10,}s  ({req_cost/3600:.1f}h)")
    print(f"  Deadhead cost     : {deadhead_cost:>10,}s  ({deadhead_cost/3600:.1f}h)")
    print(f"  Total tour cost   : {best_total:>10,}s  ({best_total/3600:.1f}h)")
    print(f"  Imbalanced nodes  : {n_imbalanced}  (total flow units: {total_supply})")
    print(f"  Lower bound days  : {best_total/MAX_DAY_SECONDS:.1f}")

    # ---------------------------------------------------------------------------
    # 4C: Local search — first-improvement direction flips
    # ---------------------------------------------------------------------------
    print()
    print("Local search (first-improvement direction flips) ...")
    t0 = time.time()
    improvements = 0

    for pass_num in range(10):
        improved = False
        for row_id in list(chosen.keys()):
            if row_id in budget_forced:
                continue          # direction is fixed by the daily budget
            u_cur, v_cur, k_cur = chosen[row_id]
            k_new = f"{row_id}_rev" if k_cur.endswith('_fwd') else f"{row_id}_fwd"
            u_new, v_new = v_cur, u_cur

            chosen[row_id] = (u_new, v_new, k_new)
            imb_new = compute_imbalances(chosen)
            req_new = arc_traversal_cost(chosen)
            _, dh_new = solve_min_cost_flow(imb_new)

            if req_new + dh_new < best_total:
                best_total = req_new + dh_new
                improvements += 1
                improved = True
            else:
                chosen[row_id] = (u_cur, v_cur, k_cur)

        if not improved:
            break

    elapsed = time.time() - t0
    print(f"  Improvements : {improvements}  ({elapsed:.1f}s,  {pass_num+1} pass(es))")

    # Recompute final solution components
    imbalance = compute_imbalances(chosen)
    flow_dict, deadhead_cost = solve_min_cost_flow(imbalance)
    req_cost = arc_traversal_cost(chosen)
    total_cost = req_cost + deadhead_cost

    print()
    print("Final solution after local search:")
    print(f"  Required arc cost : {req_cost:>10,}s  ({req_cost/3600:.1f}h)")
    print(f"  Deadhead cost     : {deadhead_cost:>10,}s  ({deadhead_cost/3600:.1f}h)")
    print(f"  Total tour cost   : {total_cost:>10,}s  ({total_cost/3600:.1f}h)")
    print(f"  Lower bound days  : {total_cost/MAX_DAY_SECONDS:.1f}")

# ---------------------------------------------------------------------------
# 4C: Build the Eulerian multigraph G_euler
# Arcs = chosen required arcs + deadhead arc-paths from flow solution.
# Deadhead paths are expanded from node sequences using APSP paths.
# ---------------------------------------------------------------------------
print()
print("Building Eulerian multigraph ...")

G_euler = nx.MultiDiGraph()
for node, data in G.nodes(data=True):
    G_euler.add_node(node, **data)

# Required arcs (one per row, chosen direction)
for row_id, (u, v, k) in chosen.items():
    arc_data = {**G[u][v][k], 'is_deadhead': False}
    G_euler.add_edge(u, v, key=f"req_{k}", **arc_data)

# Deadhead arcs: CP-SAT gives arc-level counts directly; greedy+LS needs APSP expansion
dh_serial = 0
if dh_arc_dict is not None:
    for (u, v, k), count in dh_arc_dict.items():
        for _ in range(count):
            dh_serial += 1
            G_euler.add_edge(u, v, key=f"dh_{dh_serial}",
                             **{**G[u][v][k], 'is_deadhead': True})
else:
    for s, targets in flow_dict.items():
        for t, flow_units in targets.items():
            if flow_units <= 0:
                continue
            node_path = P[s][t]
            for _ in range(int(flow_units)):
                for hop_u, hop_v, hop_k, hop_data in edge_path(G, node_path):
                    dh_serial += 1
                    G_euler.add_edge(
                        hop_u, hop_v,
                        key=f"dh_{dh_serial}",
                        **{**hop_data, 'is_deadhead': True},
                    )

n_req_arcs = sum(1 for *_, d in G_euler.edges(data=True) if not d['is_deadhead'])
n_dh_arcs  = sum(1 for *_, d in G_euler.edges(data=True) if d['is_deadhead'])
print(f"  Required arcs : {n_req_arcs}")
print(f"  Deadhead arcs : {n_dh_arcs}")
print(f"  Total arcs    : {G_euler.number_of_edges()}")

# ---------------------------------------------------------------------------
# 4D: Verify degree balance
# ---------------------------------------------------------------------------
unbalanced = {
    n: G_euler.in_degree(n) - G_euler.out_degree(n)
    for n in G_euler.nodes()
    if G_euler.in_degree(n) != G_euler.out_degree(n)
}
if unbalanced:
    print(f"\n  WARNING: {len(unbalanced)} unbalanced node(s):")
    for n, d in list(unbalanced.items())[:10]:
        print(f"    {n}  imbalance={d:+d}")
else:
    print(f"\n  All nodes degree-balanced. G_euler is ready for Eulerian circuit.")

# Costliest deadhead (display differs by method used)
print()
if dh_arc_dict is not None:
    dh_arc_summary = sorted(
        ((G[u][v][k]['weight'] * cnt, u, v, k, cnt)
         for (u, v, k), cnt in dh_arc_dict.items()),
        reverse=True,
    )
    print("Top 10 costliest deadhead arcs (CP-SAT arc-level):")
    print(f"  {'From':<8}  {'To':<8}  {'x':<3}  {'Cost':>8}")
    for cost, u, v, k, cnt in dh_arc_summary[:10]:
        print(f"  {u:<8}  {v:<8}  x{cnt:<2}  {cost:>7,}s")
else:
    dh_summary = []
    for s, targets in flow_dict.items():
        for t, units in targets.items():
            if units > 0:
                dh_summary.append((D[s][t] * units, s, t, int(units)))
    dh_summary.sort(reverse=True)
    print("Top 10 costliest deadhead segments:")
    print(f"  {'From':<8}  {'To':<8}  {'x':<3}  {'Cost':>8}  Hops  Path preview")
    for cost, s, t, n in dh_summary[:10]:
        hops = len(P[s][t]) - 1
        preview = ' -> '.join(P[s][t][:4]) + (' -> ...' if hops > 3 else '')
        print(f"  {s:<8}  {t:<8}  x{n:<2}  {cost:>7,}s  {hops:>4}  {preview}")

# ---------------------------------------------------------------------------
# Step 5 -- Eulerian Circuit Construction
# ---------------------------------------------------------------------------
# G_euler is degree-balanced; nx.eulerian_circuit uses Hierholzer's algorithm.
# Output: ordered arc list representing the full continuous walking sequence.
#
# Two variants:
#   Closed -- starts and ends at the same trailhead (standard Eulerian circuit)
#   Open   -- starts and ends at different trailheads by removing the most
#             expensive deadhead flow path, saving that travel time entirely
# ---------------------------------------------------------------------------

print()
print("=" * 60)
print("STEP 5 -- EULERIAN CIRCUIT CONSTRUCTION")
print("=" * 60)
progress(40, "Eulerian circuit construction")

# Remove isolated nodes (degree 0) — they break nx.is_eulerian()'s weak-
# connectivity check even though they don't affect the circuit itself.
# These are nodes (mostly road intersections) that appear in G but carry no
# arcs in the Eulerian tour because no required trail or deadhead path uses them.
isolated = [n for n in G_euler.nodes() if G_euler.degree(n) == 0]
G_euler.remove_nodes_from(isolated)
if isolated:
    print(f"\nRemoved {len(isolated)} isolated nodes from G_euler  "
          f"({', '.join(sorted(isolated)[:8])}{'...' if len(isolated) > 8 else ''})")

# Fix weak connectivity: some required-edge subgraphs are internally degree-balanced
# and received no deadhead flow bridging them to the main component.  Adding a
# round-trip bridge (both m->c and c->m arcs) restores weak connectivity without
# disturbing degree balance at any node.
bridge_pairs: list[tuple[str, str]] = []   # each one-way leg of every bridge
wccs = sorted(nx.weakly_connected_components(G_euler), key=len, reverse=True)
if len(wccs) > 1:
    print(f"\nWeak connectivity: {len(wccs)} components -- adding round-trip bridges ...")
    main_wcc = set(wccs[0])
    added_conn_cost = 0
    for wcc in wccs[1:]:
        rt_cost, m_node, c_node = min(
            (D[m][c] + D[c][m], m, c)
            for m in main_wcc for c in wcc
        )
        added_conn_cost += rt_cost
        for path_s, path_t in [(m_node, c_node), (c_node, m_node)]:
            bridge_pairs.append((path_s, path_t))
            for hop_u, hop_v, hop_k, hop_data in edge_path(G, P[path_s][path_t]):
                dh_serial += 1
                G_euler.add_edge(hop_u, hop_v, key=f"dh_conn_{dh_serial}",
                                 **{**hop_data, 'is_deadhead': True})
        main_wcc |= set(wcc)
        print(f"  Component ({len(wcc)} nodes): bridge {m_node}<->{c_node}, "
              f"{rt_cost:,}s ({rt_cost/3600:.2f}h)")
    print(f"  Total bridge cost added: {added_conn_cost:,}s ({added_conn_cost/3600:.2f}h)")
else:
    print("\nG_euler is weakly connected.")

assert nx.is_eulerian(G_euler), "G_euler still not Eulerian after bridge fixes"

# Select starting node: TH or CG with the highest degree in G_euler, unless
# the hiker named one.  A closed circuit is a cycle, so its start is a free
# choice among the nodes it visits -- picking a different one rotates the arc
# list and changes nothing else about the route or its cost.  Only the day
# split has to be redone, because day boundaries must land on campsites.
valid_start_nodes = [n for n, d in G.nodes(data=True)
                     if d['node_type'] in VALID_CIRCUIT_ENDPOINTS and n in G_euler]
if _args.start_node:
    START_NODE_PINNED = _args.start_node.strip().upper()
    if START_NODE_PINNED not in G.nodes:
        raise SystemExit(f"--start-node {START_NODE_PINNED!r} is not a node in "
                         f"the network")
    if G.nodes[START_NODE_PINNED]['node_type'] not in VALID_CIRCUIT_ENDPOINTS:
        raise SystemExit(f"--start-node {START_NODE_PINNED!r} is a "
                         f"{G.nodes[START_NODE_PINNED]['node_type']}; a hike can "
                         f"only begin at a trailhead or campground")
    if START_NODE_PINNED not in G_euler:
        raise SystemExit(f"--start-node {START_NODE_PINNED!r} is not on the "
                         f"circuit, so the route never passes through it")
    start_node = START_NODE_PINNED
else:
    START_NODE_PINNED = None
    start_node = max(valid_start_nodes, key=lambda n: G_euler.degree(n))
print(f"\nStarting node : {start_node}  ({G.nodes[start_node]['node_type']}, "
      f"degree {G_euler.degree(start_node)} in G_euler)")

# ---------------------------------------------------------------------------
# 5A: Closed circuit
# ---------------------------------------------------------------------------
print("\nBuilding closed circuit (Hierholzer) ...")
t0 = time.time()

circuit = [
    (u, v, k, G_euler[u][v][k])
    for u, v, k in nx.eulerian_circuit(G_euler, source=start_node, keys=True)
]

elapsed = time.time() - t0

# Sanity checks
assert len(circuit) == G_euler.number_of_edges(), \
    f"Arc count mismatch: {len(circuit)} vs {G_euler.number_of_edges()}"
assert circuit[0][0] == start_node, "Circuit does not begin at chosen start node"
assert circuit[-1][1] == start_node, "Circuit does not return to start node"

total_s   = sum(d['weight'] for _, _, _, d in circuit)
req_s     = sum(d['weight'] for _, _, _, d in circuit if not d['is_deadhead'])
dh_s_time = sum(d['weight'] for _, _, _, d in circuit if d['is_deadhead'])
n_dh_runs = sum(
    1 for i, (_, _, _, d) in enumerate(circuit)
    if d['is_deadhead'] and (i == 0 or not circuit[i-1][3]['is_deadhead'])
)

print(f"  Built in {elapsed:.2f}s")
print()
print("Closed circuit:")
print(f"  Start / End  : {start_node}")
print(f"  Total arcs   : {len(circuit)}  ({sum(1 for *_,d in circuit if not d['is_deadhead'])} required,"
      f" {sum(1 for *_,d in circuit if d['is_deadhead'])} deadhead)")
print(f"  Total time   : {total_s:>10,}s  ({total_s/3600:.1f}h)")
print(f"  Required     : {req_s:>10,}s  ({req_s/3600:.1f}h)")
print(f"  Deadhead     : {dh_s_time:>10,}s  ({dh_s_time/3600:.1f}h)  across {n_dh_runs} repositioning runs")
print(f"  Lower bound days (no campsite constraint): {total_s/MAX_DAY_SECONDS:.1f}")

# Arc preview
print()
print("First 5 arcs:")
for u, v, k, d in circuit[:5]:
    tag = "[DH]" if d['is_deadhead'] else "    "
    print(f"  {tag} {u:<8} -> {v:<8}  {d.get('trail',''):<30}  {d['weight']:>6,}s")
print("Last 5 arcs:")
for u, v, k, d in circuit[-5:]:
    tag = "[DH]" if d['is_deadhead'] else "    "
    print(f"  {tag} {u:<8} -> {v:<8}  {d.get('trail',''):<30}  {d['weight']:>6,}s")

# ---------------------------------------------------------------------------
# 5B: Open circuit -- remove the most expensive deadhead flow path
#
# Strategy: rebuild G_euler with one fewer unit of the costliest (s->t) flow,
# then find an Eulerian PATH from t to s.  This saves D[s][t] seconds and
# converts the closed loop into a walk between two distinct trailheads.
# ---------------------------------------------------------------------------
if dh_arc_dict is not None:
    # CP-SAT: remove one traversal of the heaviest deadhead arc whose endpoints
    # are valid circuit start/end nodes (TH or CG).  Cascade to relaxed filters
    # if no fully-valid arc exists.
    def _arc_weight(uvk):
        return G[uvk[0]][uvk[1]][uvk[2]]['weight']
    def _endpoint_ok(u, v, require_both=True):
        v_ok = G.nodes[v]['node_type'] in VALID_CIRCUIT_ENDPOINTS
        u_ok = G.nodes[u]['node_type'] in VALID_CIRCUIT_ENDPOINTS
        return (v_ok and u_ok) if require_both else v_ok

    cands = [k for k in dh_arc_dict if _endpoint_ok(k[0], k[1], require_both=True)]
    if not cands:
        cands = [k for k in dh_arc_dict if _endpoint_ok(k[0], k[1], require_both=False)]
    if not cands:
        cands = list(dh_arc_dict.keys())

    best_dh_u, best_dh_v, best_dh_k = max(cands, key=_arc_weight)
    best_dh_cost    = G[best_dh_u][best_dh_v][best_dh_k]['weight']
    open_walk_start = best_dh_v   # v loses an in-arc → excess out-degree
    open_walk_end   = best_dh_u   # u loses an out-arc → excess in-degree
    print()
    print(f"Open circuit: remove deadhead arc  {best_dh_u} -> {best_dh_v}"
          f"  ({best_dh_cost:,}s = {best_dh_cost/3600:.2f}h)")
else:
    def _flow_triples(require_both=True):
        for s, targets in flow_dict.items():
            for t, units in targets.items():
                if units <= 0:
                    continue
                t_ok = G.nodes[t]['node_type'] in VALID_CIRCUIT_ENDPOINTS
                s_ok = G.nodes[s]['node_type'] in VALID_CIRCUIT_ENDPOINTS
                if require_both and t_ok and s_ok:
                    yield D[s][t], s, t
                elif not require_both and t_ok:
                    yield D[s][t], s, t

    triples = list(_flow_triples(require_both=True))
    if not triples:
        triples = list(_flow_triples(require_both=False))
    if not triples:
        triples = [(D[s][t], s, t) for s, tgts in flow_dict.items()
                   for t, u in tgts.items() if u > 0]

    best_dh_cost, best_dh_s, best_dh_t = max(triples)
    open_walk_start = best_dh_t   # removing flow s->t leaves t with excess out-degree
    open_walk_end   = best_dh_s   # and s with excess in-degree
    print()
    print(f"Open circuit: remove deadhead flow  {best_dh_s} -> {best_dh_t}"
          f"  ({best_dh_cost:,}s = {best_dh_cost/3600:.2f}h)")

s_type = G.nodes[open_walk_start]['node_type']
e_type = G.nodes[open_walk_end]['node_type']
print(f"  Walk start : {open_walk_start}  ({s_type})")
print(f"  Walk end   : {open_walk_end}  ({e_type})")
print(f"  Savings    : {best_dh_cost:,}s  ({best_dh_cost/3600:.2f}h)")
print(f"  Open lower bound: {(total_s - best_dh_cost)/MAX_DAY_SECONDS:.1f} days")

# Rebuild the open Eulerian graph with one fewer unit of the removed flow path
G_euler_open = nx.MultiDiGraph()
for node, data in G.nodes(data=True):
    G_euler_open.add_node(node, **data)

for row_id, (u, v, k) in chosen.items():
    arc_data = {**G[u][v][k], 'is_deadhead': False}
    G_euler_open.add_edge(u, v, key=f"req_{k}", **arc_data)

dh_serial_open = 0
if dh_arc_dict is not None:
    skip_arc = (best_dh_u, best_dh_v, best_dh_k)
    for (u, v, k), count in dh_arc_dict.items():
        n_to_add = count - (1 if (u, v, k) == skip_arc else 0)
        for _ in range(n_to_add):
            dh_serial_open += 1
            G_euler_open.add_edge(u, v, key=f"dh_{dh_serial_open}",
                                  **{**G[u][v][k], 'is_deadhead': True})
else:
    for s, targets in flow_dict.items():
        for t, flow_units in targets.items():
            if flow_units <= 0:
                continue
            node_path = P[s][t]
            units_to_add = int(flow_units) - (
                1 if s == best_dh_s and t == best_dh_t else 0)
            for _ in range(units_to_add):
                for hop_u, hop_v, hop_k, hop_data in edge_path(G, node_path):
                    dh_serial_open += 1
                    G_euler_open.add_edge(
                        hop_u, hop_v,
                        key=f"dh_{dh_serial_open}",
                        **{**hop_data, 'is_deadhead': True},
                    )

# Apply the same connectivity bridges — WCC structure is independent of which
# flow unit we remove, so the same (m, c) pairs are needed here too.
dh_conn_serial_open = 0
for path_s, path_t in bridge_pairs:
    for hop_u, hop_v, hop_k, hop_data in edge_path(G, P[path_s][path_t]):
        dh_conn_serial_open += 1
        G_euler_open.add_edge(hop_u, hop_v, key=f"dh_conn_{dh_conn_serial_open}",
                              **{**hop_data, 'is_deadhead': True})

# Remove isolated nodes from G_euler_open (same reason as G_euler)
isolated_open = [n for n in G_euler_open.nodes() if G_euler_open.degree(n) == 0]
G_euler_open.remove_nodes_from(isolated_open)

# Verify exactly one source (open_walk_start) and one sink (open_walk_end)
imb_open = {
    n: G_euler_open.out_degree(n) - G_euler_open.in_degree(n)
    for n in G_euler_open.nodes()
    if G_euler_open.out_degree(n) != G_euler_open.in_degree(n)
}
if set(imb_open.keys()) == {open_walk_start, open_walk_end}:
    print(f"  Degree check: OK  ({open_walk_start} out+1, {open_walk_end} in+1)")
else:
    print(f"  WARNING: unexpected imbalance: {imb_open}")

# Build Eulerian path
print()
print("Building open circuit (Eulerian path) ...")
t0 = time.time()
try:
    open_circuit = [
        (u, v, k, G_euler_open[u][v][k])
        for u, v, k in nx.eulerian_path(G_euler_open, source=open_walk_start, keys=True)
    ]
    elapsed_open = time.time() - t0

    assert open_circuit[0][0]  == open_walk_start, "Open path wrong start"
    assert open_circuit[-1][1] == open_walk_end,   "Open path wrong end"

    open_total = sum(d['weight'] for _, _, _, d in open_circuit)
    print(f"  Built in {elapsed_open:.2f}s  ({len(open_circuit)} arcs)")
    print(f"  Total time : {open_total:>10,}s  ({open_total/3600:.1f}h)")
    print(f"  Lower bound: {open_total/MAX_DAY_SECONDS:.1f} days")

except Exception as exc:
    print(f"  Open path failed: {exc}")
    open_circuit = None

# ---------------------------------------------------------------------------
# 5C: Side-by-side comparison
# ---------------------------------------------------------------------------
print()
print("Closed vs Open comparison:")
print(f"  {'':30}  {'Closed':>12}  {'Open':>12}")
print(f"  {'Total time (h)':<30}  {total_s/3600:>12.1f}  "
      f"{(open_total if open_circuit else float('nan'))/3600:>12.1f}")
print(f"  {'Deadhead time (h)':<30}  {dh_s_time/3600:>12.1f}  "
      f"{sum(d['weight'] for *_,d in open_circuit if d['is_deadhead'])/3600 if open_circuit else float('nan'):>12.1f}")
print(f"  {'Lower bound days':<30}  {total_s/MAX_DAY_SECONDS:>12.1f}  "
      f"{open_total/MAX_DAY_SECONDS if open_circuit else float('nan'):>12.1f}")

# ---------------------------------------------------------------------------
# Step 6 -- Multi-Day Splitting (Hotel Selection)
# ---------------------------------------------------------------------------
# Primary solver: backward DP (optimal partition when feasible).
#   dp[i] = min days to complete arcs E[i..N-1]; valid day-end = overnight node
#   or the final arc (trailhead).
#
# Fallback: greedy forward pass with detour insertion.
#   When no overnight falls within budget on the circuit, hike as far as
#   possible, then take a there-and-back detour to the nearest overnight node.
#   The return trip is prepended to the next day's arcs.  Detour arcs are
#   tagged is_deadhead=True and their keys start with "det_".
# ---------------------------------------------------------------------------

print()
print("=" * 60)
print("STEP 6 -- MULTI-DAY SPLITTING")
print("=" * 60)
progress(45, "Multi-day splitting")

overnight_set = set(overnight_nodes)

# Exclude overnight-closed nodes -- they are legal facilities that are
# temporarily closed.  The hiker can walk through but cannot sleep there.
overnight_closed_for_sleeping = {n for n, d in G.nodes(data=True)
                                  if d.get('is_overnight_closed', False)}
if overnight_closed_for_sleeping & overnight_set:
    print(f"\nExcluding {len(overnight_closed_for_sleeping & overnight_set)} "
          f"overnight-closed node(s) from sleeping set: "
          f"{', '.join(sorted(overnight_closed_for_sleeping & overnight_set))}")
    overnight_set -= overnight_closed_for_sleeping

# Resupply node set (used by DP if MAX_DAYS_BETWEEN_RESUPPLY is set)
resupply_set = {n for n in G.nodes() if G.nodes[n].get('is_resupply', False)}

# Overnight-use constraint sets
sh_nodes     = {n for n, d in G.nodes(data=True) if d['node_type'] == 'shelter'}
single_night = sh_nodes | BC_SINGLE_NIGHT_IDS   # 1-consecutive-night cap


def find_nearest_valid_overnight(from_node, D, overnight_nodes,
                                  last_on, consec, single_night):
    """Nearest overnight that doesn't violate consecutive-stay constraints."""
    for nn in sorted(overnight_nodes, key=lambda v: D[from_node].get(v, float('inf'))):
        if nn == from_node:
            continue
        if nn == last_on and (last_on in single_night
                              or (consec >= 3 and last_on not in town_overnights)):
            continue
        dist = D[from_node].get(nn, float('inf'))
        if dist < float('inf'):
            return dist, nn
    return float('inf'), None


def day_split_dp_constrained(arc_seq, overnight_set, single_night, max_s, min_s,
                              resupply_set=None, max_days_resupply=None):
    """
    DP partition with max_s, min_s (last day exempt), and consecutive overnight
    constraints: single_night nodes cap at 1 consecutive night, other BC nodes at 3.

    Optional resupply constraint: if max_days_resupply is set, at most that
    many consecutive days may pass without the walk touching a resupply node.
    Pass-through semantics: most resupply points are trailheads where the
    hiker cannot legally overnight, so a day is credited when its walk passes
    through a resupply node, not only when it ends at one.  The hiker starts
    already supplied (days_since_resupply=0), and the final day is exempt.

    Recursion depth ~ number of days (~50-70), well within Python's limit.
    Returns list of day arc-lists, or None if infeasible.
    """
    N = len(arc_seq)
    INF = float('inf')
    memo: dict = {}
    back: dict = {}
    use_resupply = (max_days_resupply is not None and resupply_set is not None)
    if use_resupply:
        # Prefix count of resupply touches over arc to-nodes: a day spanning
        # arcs i..j touches a resupply node iff _rs_pref[j+1] - _rs_pref[i] > 0.
        # (From-nodes are covered as the previous day's to-node; the walk's
        # start node needs no credit since the hiker starts supplied.)
        _rs_pref = [0] * (N + 1)
        for _k in range(N):
            _rs_pref[_k + 1] = _rs_pref[_k] + (1 if arc_seq[_k][1] in resupply_set else 0)

    def dp(i, last_on, consec, days_since_resupply):
        if i == N:
            return 0
        key = (i, last_on, consec, days_since_resupply) if use_resupply \
              else (i, last_on, consec)
        if key in memo:
            return memo[key]
        best, best_data = INF, None
        accum = 0
        for j in range(i, N):
            accum += arc_seq[j][3]['weight']
            if accum > max_s:
                break
            if accum < min_s and j < N - 1:
                continue
            to_node = arc_seq[j][1]
            is_last = (j == N - 1)
            if not (to_node in overnight_set or is_last):
                continue
            if not is_last:
                if to_node == last_on and (
                        last_on in single_night
                        or (consec >= 3 and last_on not in town_overnights)):
                    continue
                new_on     = to_node
                new_consec = consec + 1 if to_node == last_on else 1
            else:
                new_on, new_consec = to_node, 1

            # Resupply tracking (pass-through: any touch during the day counts)
            if use_resupply:
                day_touches = (_rs_pref[j + 1] - _rs_pref[i]) > 0
                new_dsr = 0 if (day_touches or is_last) \
                            else days_since_resupply + 1
                if new_dsr > max_days_resupply:
                    continue
            else:
                new_dsr = 0

            sub = dp(j + 1, new_on, new_consec, new_dsr)
            if 1 + sub < best:
                best      = 1 + sub
                best_data = (j + 1, new_on, new_consec, new_dsr)
        memo[key] = best
        if best < INF:
            back[key] = best_data
        return best

    if dp(0, None, 0, 0) == INF:
        return None
    days, i, lo, c, dsr = [], 0, None, 0, 0
    while i < N:
        key = (i, lo, c, dsr) if use_resupply else (i, lo, c)
        j, new_lo, new_c, new_dsr = back[key]
        days.append(list(arc_seq[i:j]))
        i, lo, c, dsr = j, new_lo, new_c, new_dsr
    return days


def day_split_greedy_detour(arc_seq, overnight_set, single_night, max_s, min_s,
                             nearest_overnight_dict, P, G_full, D):
    """
    Greedy forward pass with detour insertion, enforcing min_s and consecutive
    overnight constraints.  Detour arcs are tagged is_deadhead=True.
    """
    arcs            = list(arc_seq)
    days            = []
    det_serial      = 0
    current_last_on = None
    current_consec  = 0

    i = 0
    while i < len(arcs):
        if det_serial > 5000:
            # Livelock guard: in campsite-sparse regions with tight budgets the
            # forced-detour pattern can stop consuming original arcs and insert
            # return paths forever (observed at 8h: 161M arcs, 35 GB RAM).
            return None
        accum         = 0
        last_valid    = -1
        last_valid_on = None
        last_valid_c  = 0
        budget_end    = len(arcs)

        for j in range(i, len(arcs)):
            w = arcs[j][3]['weight']
            if accum + w > max_s:
                budget_end = j
                break
            accum += w
            if accum < min_s and j < len(arcs) - 1:
                continue
            tn      = arcs[j][1]
            is_last = (j == len(arcs) - 1)
            if not (tn in overnight_set or is_last):
                continue
            if not is_last:
                if tn == current_last_on and (
                        current_last_on in single_night
                        or (current_consec >= 3
                            and current_last_on not in town_overnights)):
                    continue
            last_valid    = j
            last_valid_on = tn
            last_valid_c  = (current_consec + 1
                              if tn == current_last_on else 1)

        if last_valid >= i:
            days.append(list(arcs[i:last_valid + 1]))
            current_last_on = last_valid_on
            current_consec  = last_valid_c
            i = last_valid + 1
            continue

        # Forced detour: walk as far as possible, divert to a valid overnight
        best_k              = -1
        best_overnight_node = None
        accum2 = 0
        for k in range(i, budget_end):
            accum2 += arcs[k][3]['weight']
            tn = arcs[k][1]
            nd, nn = find_nearest_valid_overnight(
                tn, D, overnight_nodes, current_last_on, current_consec, single_night
            )
            if nn is not None and accum2 + nd <= max_s:
                best_k, best_overnight_node = k, nn

        if best_k < i:
            best_k = i
            _, best_overnight_node = find_nearest_valid_overnight(
                arcs[i][1], D, overnight_nodes,
                current_last_on, current_consec, single_night
            )
            if best_overnight_node is None:
                # Last-resort: ignore consecutive constraint
                _, best_overnight_node = nearest_overnight_dict[arcs[i][1]]

        day_arcs  = list(arcs[i:best_k + 1])
        from_node = arcs[best_k][1]
        for u, v, _, hd in edge_path(G_full, P[from_node][best_overnight_node]):
            det_serial += 1
            day_arcs.append((u, v, f"det_{det_serial}",
                             {**hd, 'is_deadhead': True}))
        days.append(day_arcs)

        current_consec  = (current_consec + 1
                           if best_overnight_node == current_last_on else 1)
        current_last_on = best_overnight_node

        resume_node = arcs[best_k + 1][0] if best_k + 1 < len(arcs) else from_node
        if resume_node != best_overnight_node:
            for idx, (u, v, _, hd) in enumerate(
                    edge_path(G_full, P[best_overnight_node][resume_node])):
                det_serial += 1
                arcs.insert(best_k + 1 + idx,
                            (u, v, f"det_{det_serial}", {**hd, 'is_deadhead': True}))

        i = best_k + 1

    return days


def fmt_hm(seconds):
    h, r = divmod(int(seconds), 3600)
    return f"{h}h{r // 60:02d}m"


def shortest_nonlast(dd):
    """Length of the shortest non-last day (the last day is exempt)."""
    pool = dd[:-1] if len(dd) > 1 else dd
    return min(sum(d['weight'] for *_, d in day) for day in pool)


def _total_walking(dd):
    """Every second the hiker is moving, deadhead included."""
    return sum(d['weight'] for day in dd for *_, d in day)


def pick_itinerary(items, days_of=lambda x: x):
    """Choose among itineraries already equal on day count.

    Prefer the least total walking -- but not at any price in day balance.
    The two pull against each other: the campsite detours that stop a 2h day
    landing beside a 12h one are the same detours that add walking, so ranking
    walking alone quietly picks the lumpiest split every time.

    Hence a floor, measured against the best candidate actually on the table
    rather than an absolute standard.  Achievable balance is set by terrain,
    not by choosing well -- the shortest non-last day runs 42-62% of the mean
    at 8h, where campsite spacing forces early stops, against 80-92% at 16h --
    so any fixed threshold is either toothless at 16h or fatal at 8h.  A 75%
    of mean floor would reject every 8h itinerary that exists.  Relative to the
    best tied candidate it self-calibrates per tier and can never reject
    everything, since the best-balanced candidate always scores 1.0.
    """
    # Fewest resupply stops first, before anything else here.  A stop is
    # hours of hitching and shopping that the model only ever sees as the
    # walking part of a detour, so it must not lose to a metric that is fully
    # measured.  Enforced at every selection point, not just the last one:
    # ranking it only across variants let the greedy floor sweep quietly buy a
    # town visit with 8.9h of walking, which is a trade the ranking says we do
    # not make.
    best_stops = min(_n_resupply_stops(days_of(i)) for i in items)
    items = [i for i in items
             if _n_resupply_stops(days_of(i)) == best_stops] or items
    best_bal = max(shortest_nonlast(days_of(i)) for i in items)
    elig = [i for i in items
            if shortest_nonlast(days_of(i)) >= BALANCE_ALPHA * best_bal] or items
    return min(elig, key=lambda i: (_total_walking(days_of(i)),
                                    -shortest_nonlast(days_of(i))))


def classify_arcs(days):
    """Walk-order traversal categories, matching the web app (docs/js/viz.js).

    The is_deadhead flag marks whichever duplicate copy balances the Euler
    tour, so a required trail's *first* physical traversal can carry
    is_deadhead=True.  Reporting instead uses what the hiker experiences:
      'new'       -- first pass of a required trail (credits map coverage)
      'connector' -- first pass of a non-required road/path
      'repeat'    -- any edge already traversed
    Returns a list of per-day lists of category strings, parallel to days.
    """
    required_eids = {d.get('edge_id') for day in days for *_, d in day
                     if not d['is_deadhead'] and d.get('edge_id') is not None}
    traversed = set()
    cats = []
    for day in days:
        day_cats = []
        for u, v, _k, d in day:
            eid  = d.get('edge_id')
            tkey = eid if eid is not None else tuple(sorted((u, v)))
            first = tkey not in traversed
            traversed.add(tkey)
            day_cats.append('repeat' if not first
                            else 'new' if eid in required_eids
                            else 'connector')
        cats.append(day_cats)
    return cats


def day_cat_seconds(day_arcs, day_cats):
    """Per-day seconds broken out by traversal category."""
    tot = {'new': 0, 'connector': 0, 'repeat': 0}
    for (_u, _v, _k, d), cat in zip(day_arcs, day_cats):
        tot[cat] += d['weight']
    return tot


def split_days_balanced(arc_seq):
    """Two-phase day split with no user-facing minimum-day parameter.

    Phase 1: DP with no daily floor -- since the DP minimizes day count,
    a floor can only cost days, so this yields the true minimum count.
    Phase 2: binary-search the largest daily floor (last day exempt) that
    still admits a split at that count, so the shortest day is as long as
    possible without giving up a single day of optimality.
    Returns (days, floor_seconds), or (None, None) if no floor-0 split
    exists (an overnight gap exceeds the daily budget; detours needed).
    """
    base = day_split_dp_constrained(arc_seq, overnight_set, single_night,
                                    MAX_DAY_SECONDS, 0,
                                    resupply_set, MAX_DAYS_BETWEEN_RESUPPLY)
    if base is None:
        return None, None
    target = len(base)
    lo, hi, best = 0, MAX_DAY_SECONDS, base
    while hi - lo > 60:                      # 1-minute precision
        mid = (lo + hi) // 2
        trial = day_split_dp_constrained(arc_seq, overnight_set, single_night,
                                         MAX_DAY_SECONDS, mid,
                                         resupply_set, MAX_DAYS_BETWEEN_RESUPPLY)
        if trial is not None and len(trial) == target:
            best, lo = trial, mid
        else:
            hi = mid
    return best, lo


def strip_redundant_deadhead_loops(days):
    """Delete closed all-deadhead loops stranded inside a day.

    Both detour remedies size their out-and-backs for the day boundaries they
    assume: day_split_greedy_detour appends "walk to camp" to the day it is
    building and pushes "camp back to the walk" into the next one, and
    insert_overnight_detours plans out-and-backs meant to be cut in half by a
    day end at the camp.  The exact DP then re-splits the augmented walk from
    scratch and is free to put the boundary somewhere else entirely -- when it
    does, the detour survives whole, in the middle of a day, as a loop that
    leaves a node and returns to it having covered no required edge.  Nothing
    downstream removed those, so they shipped as real repeat walking (Day 29 of
    16h Open climbed Spruce Mountain to campsite 42 and back down for nothing).

    Removal is safe by construction: every dropped arc is a deadhead (required
    coverage cannot change), the loop's anchor node stays in the sequence (the
    day's start and end nodes, hence the overnight sequence and its
    consecutive-night bookkeeping, are untouched), and a day only gets shorter,
    so no daily budget can be broken.  The one thing a removal can cost is a
    resupply touch -- resupply is credited pass-through, anywhere in the day --
    so a loop is kept whenever dropping it would change the day's set of
    resupply nodes.  Returns (days, n_removed, seconds_removed).
    """
    out, n_removed, s_removed = [], 0, 0
    for day in days:
        arcs = list(day)
        while len(arcs) > 1:
            seq = [arcs[0][0]] + [a[1] for a in arcs]
            rs_before = {n for n in seq[1:] if n in resupply_set}
            cut = None
            for i in range(len(arcs)):
                # Longest loop first: one deletion instead of several nested.
                for j in range(len(arcs), i, -1):
                    if seq[j] != seq[i] or j - i == len(arcs):
                        continue
                    if not all(a[3].get('is_deadhead') for a in arcs[i:j]):
                        continue
                    kept = seq[:i] + seq[j:]
                    if {n for n in kept[1:] if n in resupply_set} != rs_before:
                        continue
                    cut = (i, j)
                    break
                if cut:
                    break
            if not cut:
                break
            i, j = cut
            n_removed += 1
            s_removed += sum(a[3]['weight'] for a in arcs[i:j])
            del arcs[i:j]
        out.append(arcs)
    return out, n_removed, s_removed


def insert_resupply_detours(arc_seq, rs_nodes, max_days, max_day_s, label,
                            objective='fewest'):
    """Pre-process a walk so a resupply window is achievable by day-splitting.

    The Euler tour is built with no knowledge of resupply, so it can go 100h+
    between resupply touches -- no day re-slicing can fix that.  This inserts
    out-and-back deadhead detours to reachable resupply nodes wherever the
    walk would otherwise exceed max_days day-budgets (max_days * max_day_s
    walking seconds) since its last touch.  The hiker starts fully supplied,
    so the first window is measured from the walk's start.

    objective='fewest'   minimize (number of detours, walking time) -- most
                         resupply points are outside the park, so each stop
                         carries town-access overhead the graph can't see.
    objective='cheapest' minimize (walking time, number of detours) -- the
                         pre-2026-07-06 behavior, kept as an alternate
                         candidate since less added walking can save a day.
    """
    INF    = float('inf')
    target = max_days * max_day_s

    # Up to 3 cheapest round-trip resupply options per node (in-park nodes
    # win exact cost ties -- no town-access overhead)
    rt_opts: dict[str, list] = {}
    def rt_options(node):
        if node not in rt_opts:
            opts = sorted(
                ((D[node].get(r, INF) + D[r].get(node, INF),
                  0 if r in IN_PARK_RESUPPLY else 1,
                  D[node].get(r, INF), D[r].get(node, INF), r) for r in rs_nodes))
            rt_opts[node] = [(o[0], o[2], o[3], o[4]) for o in opts[:3] if o[0] < INF]
        return rt_opts[node]

    # Split the walk into stretches between natural resupply touches.
    stretches, cur = [], []
    for arc in arc_seq:
        cur.append(arc)
        if arc[1] in rs_nodes:
            stretches.append(cur)
            cur = []
    if cur:                    # trailing stretch: no touch at its end
        stretches.append(cur)

    # Plan score is (n_stops, walking_s); rank() orders it per the objective.
    NOPLAN = (INF, INF)
    rank = (lambda s: s) if objective == 'fewest' else (lambda s: (s[1], s[0]))

    def plan(arcs):
        """Best insertions for one touch-to-touch stretch: shortest path
        over (boundary, resupply node) candidates such that no walk-time gap
        between consecutive touches exceeds target, minimizing rank(score).
        Returns [(idx, node)] (detour before arc idx), [] if none needed,
        or None if infeasible."""
        S = sum(d['weight'] for *_, d in arcs)
        if S <= target:
            return []
        cands, T = [], 0
        for idx, (u, _v, _k, d) in enumerate(arcs):
            for cost, out, back, r in rt_options(u):
                cands.append((T, idx, cost, out, back, r))
            T += d['weight']
        best   = [NOPLAN] * len(cands)
        parent = [-1] * len(cands)
        for i, (T_i, idx_i, c_i, o_i, _b_i, _r_i) in enumerate(cands):
            if T_i + o_i <= target:
                best[i] = (1, c_i)
            for j in range(i):
                T_j, idx_j, _c_j, _o_j, b_j, _r_j = cands[j]
                if best[j] == NOPLAN:
                    continue
                via = (best[j][0] + 1, best[j][1] + c_i)
                if (idx_j < idx_i
                        and (T_i - T_j) + b_j + o_i <= target
                        and rank(via) < rank(best[i])):
                    best[i], parent[i] = via, j
        goal, goal_score = -1, NOPLAN
        for i, (T_i, _idx_i, _c_i, _o_i, b_i, _r_i) in enumerate(cands):
            if (best[i] != NOPLAN and rank(best[i]) < rank(goal_score)
                    and (S - T_i) + b_i <= target):
                goal, goal_score = i, best[i]
        if goal < 0:
            return None
        chain = []
        while goal >= 0:
            chain.append((cands[goal][1], cands[goal][5]))
            goal = parent[goal]
        return chain[::-1]

    inserts = []               # (global_pos, rs_node)
    base = 0
    for arcs in stretches:
        planned = plan(arcs)
        if planned is None:
            print(f"  {label}: no feasible resupply detour plan for a "
                  f"{sum(d['weight'] for *_, d in arcs) / 3600:.0f}h stretch "
                  f"-- giving up")
            return arc_seq
        inserts += [(base + idx, r) for idx, r in planned]
        base += len(arcs)

    if not inserts:
        return arc_seq

    # Phase 2: splice the round trips into the walk.
    by_pos: dict[int, list[str]] = {}
    for ipos, r in inserts:
        by_pos.setdefault(ipos, []).append(r)
    out, serial, det_s = [], 0, 0
    for pos, arc in enumerate(arc_seq):
        for r in by_pos.get(pos, []):
            n = arc[0]
            for leg in (P[n][r], P[r][n]):
                for au, av, _k, hd in edge_path(G, leg):
                    serial += 1
                    det_s  += hd['weight']
                    out.append((au, av, f"rsdet_{serial}", {**hd, 'is_deadhead': True}))
        out.append(arc)

    print(f"  {label}: inserted {len(inserts)} resupply detour(s) "
          f"[{objective}], +{det_s / 3600:.1f}h repeat walking:")
    for ipos, r in inserts:
        n = arc_seq[ipos][0]
        print(f"    at {n}: out-and-back to {r}  "
              f"({(D[n][r] + D[r][n]) / 3600:.1f}h round trip)")
    return out


def planned_resupply_schedule(days, rs_set, max_days):
    """Minimal actual-resupply schedule over a finished day split.

    Days that touch a resupply node are opportunities, not obligations: the
    hiker starts fully supplied and only stops when the next max_days window
    could not otherwise be covered.  Greedy latest-opportunity is optimal for
    minimizing stop count and spaces stops as close to max_days apart as the
    walk allows.  When the chosen day touches several resupply nodes, prefer
    an in-park one (no town-access overhead).  The final day is exempt.

    Returns [(day_num, node, days_since_last_stop)] or None if no schedule
    exists (only possible if the split itself violates the window).

    days_since_last_stop is the DAY-NUMBER difference (stop day minus the
    previous stop day, or minus 0 for the first stop).  Because resupplies
    happen mid-walk, the count of full days hiked WITHOUT resupply between
    two stops is one less: stops on days 4 and 11 mean 6 dry days (5-10),
    satisfying max_days=6.  Human-facing output should report the
    "full days without resupply" figure (value - 1); the raw difference is
    what gets exported as days_since_last in the preset JSONs.
    """
    n = len(days)
    touches = {}
    for day_num, day_arcs in enumerate(days, 1):
        t = {v for _u, v, _k, _d in day_arcs} & rs_set
        if t:
            touches[day_num] = t
    stops, last = [], 0
    while n - last - 1 > max_days:          # days last+1..n-1 must be covered
        cand = [d for d in range(last + 1, last + max_days + 2)
                if d in touches and d < n]
        if not cand:
            return None
        d = max(cand)
        node = min(touches[d], key=lambda x: (x not in IN_PARK_RESUPPLY, x))
        stops.append((d, node, d - last))
        last = d
    return stops


def _n_resupply_stops(days):
    """Planned town visits.  Defined up here because pick_itinerary ranks on
    it, and that runs inside the day-split loop."""
    if MAX_DAYS_BETWEEN_RESUPPLY is None:
        return 0
    sched = planned_resupply_schedule(days, resupply_set,
                                      MAX_DAYS_BETWEEN_RESUPPLY)
    return len(sched) if sched is not None else float('inf')


def insert_overnight_detours(arc_seq, max_day_s, label):
    """When a camp-to-camp gap along the walk exceeds the daily budget, no
    detour-free day split exists and the greedy fallback can livelock in
    campsite-sparse regions (see guard in day_split_greedy_detour).  Instead,
    plan minimum-cost out-and-back campsite detours directly: chain
    (arc boundary, campsite) candidates so consecutive day-end opportunities
    are at most one day-budget apart (back_prev + walk + out_next <= budget),
    minimizing total detour walking -- the same shortest-path structure as
    insert_resupply_detours, with on-walk campsites as zero-cost candidates.
    The exact DP then splits days with no greedy floor sweep.
    Returns the augmented walk, or None if no feasible plan (or none needed).
    """
    INF = float('inf')

    on_opts: dict[str, list] = {}
    def options(node):
        # Camps ranked by out-leg, back-leg, AND round trip: chain feasibility
        # can hinge on a short one-way leg (laddering from the same camp) even
        # when its round trip is not among the cheapest.
        if node not in on_opts:
            if node in overnight_set:
                on_opts[node] = [(0, 0, 0, node)]
            else:
                # Iterate the sorted node list, not the set: Python
                # randomises string hashing per process, so set order -- and
                # therefore which four camps survive each [:4] below -- would
                # otherwise change between runs of the same input.  The sort
                # keys carry the node id as a final tiebreaker for the same
                # reason.
                triples = [(D[node].get(c, INF), D[c].get(node, INF), c)
                           for c in sorted(overnight_set)]
                keep = set()
                for keyfn in (lambda t: (t[0], t[2]), lambda t: (t[1], t[2]),
                              lambda t: (t[0] + t[1], t[2])):
                    keep.update(sorted(triples, key=keyfn)[:4])
                on_opts[node] = sorted((o + b, o, b, c) for o, b, c in keep
                                       if o + b < INF)
        return on_opts[node]

    cands, T = [], 0
    for idx, (u, _v, _k, d) in enumerate(arc_seq):
        for cost, out, back, c in options(u):
            cands.append((T, idx, cost, out, back, c))
        T += d['weight']
    S = T

    import bisect as _bisect
    Ts     = [c[0] for c in cands]
    best   = [INF] * len(cands)
    parent = [-1] * len(cands)
    for i, (T_i, idx_i, c_i, o_i, _b_i, _cn) in enumerate(cands):
        if T_i + o_i <= max_day_s:
            best[i] = c_i
        # only candidates within one day-budget of walking can chain to i
        for j in range(_bisect.bisect_left(Ts, T_i - max_day_s), i):
            T_j, idx_j, _c_j, _o_j, b_j, cn_j = cands[j]
            if (best[j] < INF and idx_j < idx_i
                    and (T_i - T_j) + b_j + o_i <= max_day_s
                    and not (cn_j == _cn and _cn in single_night)
                    and best[j] + c_i < best[i]):
                best[i], parent[i] = best[j] + c_i, j
    goal, goal_cost = -1, INF
    for i, (T_i, _idx_i, _c_i, _o_i, b_i, _cn) in enumerate(cands):
        if best[i] < goal_cost and (S - T_i) + b_i <= max_day_s:
            goal, goal_cost = i, best[i]
    if goal < 0:
        reach = [(cands[i][0], i) for i in range(len(cands)) if best[i] < INF]
        if reach:
            T_max, i_max = max(reach)
            _T, idx_m, _c, _o, _b, cn_m = cands[i_max]
            print(f"  {label}: no feasible campsite detour plan -- chain stalls "
                  f"at t={T_max / 3600:.1f}h of {S / 3600:.1f}h "
                  f"(arc {idx_m}, node {arc_seq[idx_m][0]}, last camp {cn_m})")
        else:
            print(f"  {label}: no feasible campsite detour plan -- "
                  f"no first-day candidate")
        return None

    chain = []
    g = goal
    while g >= 0:
        chain.append(g)
        g = parent[g]
    chain.reverse()
    picks = [(cands[g][1], cands[g][5]) for g in chain if cands[g][2] > 0]
    if not picks:
        return None            # zero-insertion chain: nothing to add

    by_pos: dict[int, list[str]] = {}
    for ipos, c in picks:
        by_pos.setdefault(ipos, []).append(c)
    out, serial, det_s = [], 0, 0
    for pos, arc in enumerate(arc_seq):
        for c in by_pos.get(pos, []):
            n = arc[0]
            for leg in (P[n][c], P[c][n]):
                for au, av, _k, hd in edge_path(G, leg):
                    serial += 1
                    det_s  += hd['weight']
                    out.append((au, av, f"ondet_{serial}", {**hd, 'is_deadhead': True}))
        out.append(arc)

    print(f"  {label}: planned {len(picks)} campsite detour(s), "
          f"+{det_s / 3600:.1f}h repeat walking:")
    for ipos, c in picks:
        n = arc_seq[ipos][0]
        print(f"    at {n}: out-and-back to {c}  "
              f"({(D[n][c] + D[c][n]) / 3600:.1f}h round trip)")
    return out


def retarget_termini_for_budget(circuit, open_circuit):
    """A tight daily budget can make a required arc impossible inside ANY
    interior day: nearest-camp access + traversal exceeds the budget (at 8h,
    Cove Mountain TH106-TI086 needs >=9.4h camp-to-camp).  Such an arc is only
    coverable on day 1 (or the last day), which start/end at a walk terminus
    instead of a camp.  Rotate the closed circuit so it starts with that arc,
    and re-open the open walk by dropping the deadhead arc arriving at the
    new start.  No day-splitting strategy can substitute for this: the arc is
    atomic (no interior nodes), so terminus placement is the only rescue.
    """
    if circuit is None:
        return circuit, open_circuit
    if START_NODE_PINNED is not None:
        # Rotating would move day 1 away from the trailhead the hiker asked
        # for, which is the one thing they explicitly chose.  Honour the pin
        # and say plainly that it may cost feasibility, rather than quietly
        # starting them somewhere else.
        print(f"\nBudget retarget: skipped -- start pinned to "
              f"{START_NODE_PINNED}.  If a required arc cannot fit any interior "
              f"day at {MAX_DAY_SECONDS / 3600:.0f}h, this configuration will "
              f"yield no itinerary; an unpinned start could rescue it.")
        return circuit, open_circuit
    INF = float('inf')
    _out, _back = {}, {}
    def out_of(n):
        if n not in _out:
            _out[n] = min(D[n].get(c, INF) for c in overnight_set)
        return _out[n]
    def back_of(n):
        if n not in _back:
            _back[n] = min(D[c].get(n, INF) for c in overnight_set)
        return _back[n]

    bad = [i for i, (u, v, _k, d) in enumerate(circuit)
           if not d.get('is_deadhead')
           and back_of(u) + d['weight'] + out_of(v) > MAX_DAY_SECONDS]
    if not bad:
        return circuit, open_circuit

    print(f"\nBudget retarget: {len(bad)} required arc(s) cannot fit an "
          f"interior day at {MAX_DAY_SECONDS / 3600:.0f}h:")
    for i in bad:
        u, v, _k, d = circuit[i]
        print(f"  {u}->{v}  {d.get('trail', '?')}  "
              f"(camp-to-camp {(back_of(u) + d['weight'] + out_of(v)) / 3600:.1f}h)")
    if len(bad) > 1:
        print("  more than one such arc -- rotation can rescue only one; "
              "leaving circuits unchanged")
        return circuit, open_circuit

    k = bad[0]
    u, v, _k2, d = circuit[k]
    if d['weight'] + out_of(v) > MAX_DAY_SECONDS:
        print(f"  even a day-1 start at {u} cannot fit {u}->{v} -- "
              f"leaving circuits unchanged")
        return circuit, open_circuit

    rotated = circuit[k:] + circuit[:k]
    print(f"  rotated closed circuit to start at {u}: {d.get('trail', '?')} "
          f"is day 1 ({(d['weight'] + out_of(v)) / 3600:.1f}h to first camp)")

    last = rotated[-1]
    nxt  = rotated[1] if len(rotated) > 1 else None
    if last[3].get('is_deadhead'):
        new_open = rotated[:-1]
        print(f"  open circuit: dropped deadhead {last[0]}->{last[1]} "
              f"({last[3]['weight']}s) -- walk runs {u} .. {last[0]} "
              f"with {d.get('trail', '?')} first")
    elif (nxt is not None and nxt[3].get('is_deadhead')
          and back_of(u) + d['weight'] <= MAX_DAY_SECONDS):
        # No deadhead arrives at u, but one leaves v: drop it instead, making
        # the bad arc the walk's LAST arc (final day: camp -> u -> v -> done).
        new_open = rotated[2:] + [rotated[0]]
        print(f"  open circuit: dropped deadhead {nxt[0]}->{nxt[1]} "
              f"({nxt[3]['weight']}s) -- walk runs {nxt[1]} .. {u} .. {v} "
              f"with {d.get('trail', '?')} last "
              f"({(back_of(u) + d['weight']) / 3600:.1f}h final day)")
    else:
        new_open = open_circuit
        print(f"  open circuit: no deadhead arc adjacent to {u}->{v} -- "
              f"keeping original open termini")
    return rotated, new_open


def _open_trim_cuts(arcs):
    """How many deadhead arcs may be cut from each end of an open walk.

    A leading run of deadhead arcs on an open walk is free to delete: the walk
    still covers every required arc, and the hiker simply starts where the run
    ended.  The same holds at the tail.  The only real constraint is somewhere
    to be dropped off or collected, so cut only as deep as leaves the walk at a
    trailhead or campground -- and take the deepest legal cut, since the first
    one is not always the best.

    Returns (cut_head, cut_tail, seconds_saved).
    """
    def _ok(n):
        return node_type(n) in VALID_CIRCUIT_ENDPOINTS

    lead = 0
    while lead < len(arcs) and arcs[lead][3].get('is_deadhead'):
        lead += 1
    # Dropping the first i arcs makes the head of arc i-1 the new start node.
    cut_head = max((i for i in range(1, lead + 1) if _ok(arcs[i - 1][1])),
                   default=0)

    tail = 0
    while (tail < len(arcs) - cut_head
           and arcs[len(arcs) - 1 - tail][3].get('is_deadhead')):
        tail += 1
    # Dropping the last j arcs makes the tail of arc -j the new end node.
    cut_tail = max((j for j in range(1, tail + 1) if _ok(arcs[len(arcs) - j][0])),
                   default=0)

    saved = (sum(d['weight'] for *_, d in arcs[:cut_head])
             + sum(d['weight'] for *_, d in arcs[len(arcs) - cut_tail:]))
    return cut_head, cut_tail, saved


def trim_open_arcs(seq):
    """Trim an open walk before it is split into days.

    The default 12h itinerary used to begin with 0.9mi of Heintooga Ridge Road,
    covering nothing, purely because nx.eulerian_path happened to leave the
    source on that arc rather than on Flat Creek.  All 57 open presets began or
    ended on a deadhead this way, 18.4h of walking in total.

    Trimming here rather than after the split hands the freed time to the
    day-split DP, which can then repack -- worth a whole day on four resupply
    configurations.  That is only safe because the chooser now ranks total
    walking behind a balance floor; without it, re-splitting reshuffled
    campsite detours blindly and added up to 2.6h of walking to six 16h
    itineraries that saved no days at all.
    """
    if not seq:
        return seq
    cut_head, cut_tail, saved = _open_trim_cuts(seq)
    if not cut_head and not cut_tail:
        return seq
    trimmed = seq[cut_head:len(seq) - cut_tail]
    print(f"\nOpen termini trim: dropped {cut_head} leading + {cut_tail} "
          f"trailing deadhead arc(s) before splitting, {saved:,}s "
          f"({saved / 3600:.2f}h)")
    print(f"  walk now runs {trimmed[0][0]} .. {trimmed[-1][1]}, opening on "
          f"{trimmed[0][3].get('trail', '?')} and closing on "
          f"{trimmed[-1][3].get('trail', '?')}")
    return trimmed


def trim_open_termini(days):
    """Safety net: trim the ends of an already-split open itinerary.

    trim_open_arcs has normally done this already, so this is usually a no-op.
    It runs anyway because resupply and campsite detour insertion both rewrite
    the walk after that point and could in principle reintroduce a deadhead
    end.  It also repairs an illegal terminus that predates either trim:
    retarget_termini_for_budget rotates the walk and drops an arc without
    rechecking node type, which is how 8h came to finish at RI109, a road
    intersection no one can be collected from.
    """
    if not days:
        return days
    flat = [a for day in days for a in day]
    cut_head, cut_tail, saved = _open_trim_cuts(flat)
    if not cut_head and not cut_tail:
        return days

    idx = [di for di, day in enumerate(days) for _ in day]
    keep = list(zip(idx, flat))[cut_head:len(flat) - cut_tail]
    trimmed = [day for day in
               ([a for di, a in keep if di == i] for i in range(len(days)))
               if day]
    print(f"\nOpen termini trim (post-split): dropped {cut_head} leading + "
          f"{cut_tail} trailing deadhead arc(s), {saved:,}s ({saved / 3600:.2f}h)")
    print(f"  walk now runs {trimmed[0][0][0]} .. {trimmed[-1][-1][1]}, opening "
          f"on {trimmed[0][0][3].get('trail', '?')} and closing on "
          f"{trimmed[-1][-1][3].get('trail', '?')}")
    if len(days) - len(trimmed):
        print(f"  {len(days) - len(trimmed)} day(s) were entirely deadhead "
              f"and are gone")
    return trimmed


circuit, open_circuit = retarget_termini_for_budget(circuit, open_circuit)

# Env switch exists only so the batch below can measure trim placement
# both ways; pre-split is the intended behaviour.
if os.environ.get('SMOKIES_TRIM_PRESPLIT', '1') == '1':
    open_circuit = trim_open_arcs(open_circuit)

# Run the balanced day split for closed and open circuits; campsite-detour
# planning first, greedy+detour sweep as fallback, if no floor-0 split exists.
# With a resupply window, detour insertion runs under both objectives
# (fewest stops / cheapest walking) and each result is split independently;
# the day count decides between them, then fewer planned resupply stops,
# then the longer shortest non-last day.
dp_results: dict[str, list | None] = {}
_split_jobs: list = []
for base_label, base_seq in [("Closed", circuit), ("Open", open_circuit)]:
    if base_seq is None:
        dp_results[base_label] = None
        continue
    if MAX_DAYS_BETWEEN_RESUPPLY is not None:
        variants = []
        for _obj in ('fewest', 'cheapest'):
            seq = insert_resupply_detours(base_seq, resupply_set,
                                          MAX_DAYS_BETWEEN_RESUPPLY,
                                          MAX_DAY_SECONDS, base_label,
                                          objective=_obj)
            sig = tuple((u, v) for u, v, _k, _d in seq)
            if all(sig != s for _o, s, _q in variants):
                variants.append((_obj, sig, seq))
        _split_jobs += [(base_label, _obj, seq) for _obj, _sig, seq in variants]
    else:
        _split_jobs.append((base_label, '', base_seq))

_split_cands: dict[str, list] = {}
_n_jobs = max(1, len(_split_jobs))
for _job_i, (base_label, _tag, arc_seq) in enumerate(_split_jobs):
    label = f"{base_label}[{_tag}]" if _tag else base_label
    progress(45 + 47 * _job_i / _n_jobs, f"Splitting days ({label})")

    def _clean(cand_days, why):
        """Strip stranded detour loops before this candidate is scored, so the
        day-count / shortest-day comparison below ranks real itineraries."""
        if cand_days is None:
            return None
        cleaned, n_cut, s_cut = strip_redundant_deadhead_loops(cand_days)
        if n_cut:
            print(f"  {label}: {why}: dropped {n_cut} stranded deadhead "
                  f"loop(s), -{s_cut / 3600:.1f}h repeat walking")
        return cleaned

    days_base, floor_base = split_days_balanced(arc_seq)
    days_base = _clean(days_base, "detour-free split")
    if days_base is not None:
        print(f"\n{label}: detour-free DP split -> {len(days_base)} days "
              f"(shortest non-last day {fmt_hm(shortest_nonlast(days_base))})")
    else:
        print(f"\n{label}: no detour-free DP split")

    # Detour insertion can reduce the day count even when a detour-free split
    # exists (a detour repositions a day end so later days pack fuller), so
    # always evaluate both remedies and keep the best result:
    #   (a) exact campsite-detour plan + DP -- fast, and immune to the greedy
    #       livelock in campsite-sparse regions at tight budgets;
    #   (b) greedy floor sweep -- where the greedy heuristic inserts detours
    #       depends heavily on its floor parameter, so sweep the floor
    #       internally and refine each candidate with the exact DP.
    days_plan, floor_plan = None, None
    planned_seq = insert_overnight_detours(arc_seq, MAX_DAY_SECONDS, label)
    if planned_seq is not None:
        days_plan, floor_plan = split_days_balanced(planned_seq)
        days_plan = _clean(days_plan, "campsite-detour plan")
        if days_plan is not None:
            print(f"  {label}: campsite-detour plan -> {len(days_plan)} days "
                  f"(shortest non-last day {fmt_hm(shortest_nonlast(days_plan))})")

    print(f"{label}: sweeping greedy detour floors")
    candidates = []        # (n_days, floor_frac, augmented_seq, greedy_days)
    _floors = (0.0, 0.3, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 5/6, 0.85, 0.9)
    for _fi, frac in enumerate(_floors):
        # The floor sweep is the slow tail: under a wall-clock budget, stop
        # early and let the chooser below pick from what we have (the base
        # DP and campsite-plan candidates are already computed and cheap).
        if time_left() < 5:
            BUDGET_TRUNCATED = True
            print(f"    time budget exhausted -- stopping floor sweep early "
                  f"({len(candidates)} candidate(s) kept)")
            break
        progress(45 + 47 * (_job_i + 0.2 + 0.8 * _fi / len(_floors)) / _n_jobs,
                 f"Splitting days ({label}, floor {frac:.0%})")
        cand = day_split_greedy_detour(arc_seq, overnight_set, single_night,
                                       MAX_DAY_SECONDS, int(frac * MAX_DAY_SECONDS),
                                       nearest_overnight, P, G, D)
        if cand is None:
            continue
        aug     = [arc for day in cand for arc in day]
        refined = day_split_dp_constrained(aug, overnight_set, single_night,
                                           MAX_DAY_SECONDS, 0,
                                           resupply_set, MAX_DAYS_BETWEEN_RESUPPLY)
        if refined is None and MAX_DAYS_BETWEEN_RESUPPLY is not None:
            # The greedy heuristic is resupply-blind.  Resupply detours were
            # planned into arc_seq before the sweep, but the greedy pass then
            # inserts camp detours of its own, which stretch the walking time
            # between resupply touches and can push a gap past the window --
            # so every floor dies here and the configuration yields nothing at
            # all.  Six of them did: 8h with r4/r7/r8, and three others.
            #
            # The augmented walk is still a valid walk, so re-plan resupply on
            # it and try once more.  This is the same routine that seeded
            # arc_seq, now applied to what the greedy actually produced.
            repaired = insert_resupply_detours(aug, resupply_set,
                                               MAX_DAYS_BETWEEN_RESUPPLY,
                                               MAX_DAY_SECONDS,
                                               f"{label} floor {frac:.0%}",
                                               objective='fewest')
            if repaired is not aug:
                refined = day_split_dp_constrained(
                    repaired, overnight_set, single_night, MAX_DAY_SECONDS, 0,
                    resupply_set, MAX_DAYS_BETWEEN_RESUPPLY)
                if refined is not None:
                    aug = repaired
                    print(f"    floor {frac:>5.0%}: resupply re-planned on the "
                          f"augmented walk -> {len(refined)} days")
            if refined is None:
                print(f"    floor {frac:>5.0%}: no resupply-feasible split -- skipped")
                continue
        n = len(refined) if refined is not None else len(cand)
        n_det = sum(1 for _, _, k, _ in aug if k.startswith("det_"))
        print(f"    floor {frac:>5.0%}: {n} days  ({n_det} detour arcs)")
        candidates.append((n, frac, aug, cand))

    # Tie-break: each tied candidate has a different detour placement, so
    # balance every one and keep the itinerary whose shortest non-last day
    # is longest.  Physically identical sequences are deduplicated first.
    ties = []
    if not candidates:
        print(f"  {label}: greedy sweep produced no usable candidate")
    else:
        n_best = min(c[0] for c in candidates)
        seen_sigs = set()
        for c in (c for c in candidates if c[0] == n_best):
            sig = hash(tuple((u, v) for u, v, _, _ in c[2]))
            if sig not in seen_sigs:
                seen_sigs.add(sig)
                ties.append(c)

    days, best_frac, method = None, None, None
    _pool = []
    for n, frac, aug, greedy_days in ties:
        days_bal, _fs = split_days_balanced(aug)
        if days_bal is not None and len(days_bal) == n_best:
            cand_days = days_bal
        elif len(greedy_days) == n_best:
            cand_days = greedy_days
        else:
            continue
        cand_days = _clean(cand_days, f"greedy floor {frac:.0%}")
        print(f"    tie @ floor {frac:>5.0%}: shortest non-last day "
              f"{fmt_hm(shortest_nonlast(cand_days))}, "
              f"{_total_walking(cand_days) / 3600:.1f}h walking")
        _pool.append((cand_days, frac))
    if _pool:
        days, best_frac = pick_itinerary(_pool, days_of=lambda c: c[0])
        if len(_pool) > 1:
            _bb = max(shortest_nonlast(c[0]) for c in _pool)
            _kept = sum(1 for c in _pool
                        if shortest_nonlast(c[0]) >= BALANCE_ALPHA * _bb)
            print(f"    -> floor {best_frac:.0%} wins: "
                  f"{len(_pool) - _kept} of {len(_pool)} cut by the "
                  f"{BALANCE_ALPHA:.0%} balance floor, least walking of the rest")

    if days is None and ties:              # safety net: first tied greedy split
        days, best_frac = _clean(ties[0][3], "greedy fallback"), ties[0][1]
    if days is not None:
        method = (f"greedy+detour (floor {best_frac:.0%}) -> DP balanced, "
                  f"best of {len(ties)} tie(s)")

    # Keep the best remedy: fewest days first, then the shared rule.  This
    # replaces three hand-written tie-breaks that each expressed a piece of the
    # same intent -- including "the detour-free base wins full ties since it
    # adds no walking", which minimising walking now says directly.
    _remedies = []
    if days is not None:
        _remedies.append((days, method))
    if days_plan is not None:
        _remedies.append((days_plan, "campsite-detour plan -> DP (min days)"))
    if days_base is not None:
        _remedies.append((days_base, "DP (min days)"))
    if _remedies:
        _fewest = min(len(r[0]) for r in _remedies)
        days, method = pick_itinerary([r for r in _remedies
                                       if len(r[0]) == _fewest],
                                      days_of=lambda r: r[0])

    if days is None:
        print(f"  {label}: no valid split found")
        continue

    t  = sum(d['weight'] for day in days for _, _, _, d in day)
    shortest = (min(sum(d['weight'] for *_, d in day) for day in days[:-1])
                if len(days) > 1 else sum(d['weight'] for *_, d in days[-1]))
    print(f"  {label} [{method}]: {len(days)} days  "
          f"({t / 3600:.1f}h total, shortest non-last day {fmt_hm(shortest)})")
    _split_cands.setdefault(base_label, []).append((days, _tag))

for base_label in ("Closed", "Open"):
    if base_label in dp_results:            # circuit itself was absent
        continue
    cands = _split_cands.get(base_label, [])
    if not cands:
        dp_results[base_label] = None
        continue
    # Rank: fewest days, then fewest resupply stops -- a stop is hours of
    # hitching and shopping the model only sees as the walking part of a
    # detour, so nothing cheaper-to-measure may outrank it.  Everything still
    # tied then goes through the shared balance-floor / least-walking rule.
    def _primary(c):
        return (len(c[0]), _n_resupply_stops(c[0]))
    _best_primary = min(_primary(c) for c in cands)
    _tied = [c for c in cands if _primary(c) == _best_primary]
    days, tag = pick_itinerary(_tied, days_of=lambda c: c[0])
    if len(_tied) > 1:
        _bb = max(shortest_nonlast(c[0]) for c in _tied)
        _kept = sum(1 for c in _tied
                    if shortest_nonlast(c[0]) >= BALANCE_ALPHA * _bb)
        print(f"  {base_label}: {len(_tied)} variant(s) tied on "
              f"{_best_primary[0]} days / {_best_primary[1]} resupply stop(s); "
              f"{len(_tied) - _kept} cut by the {BALANCE_ALPHA:.0%} balance "
              f"floor, kept [{tag}] at {_total_walking(days) / 3600:.1f}h walking")
    if base_label == "Open":
        days = trim_open_termini(days)
    dp_results[base_label] = days
    if len(cands) > 1:
        print(f"  {base_label}: kept [{tag}] variant -- {len(days)} days, "
              f"{_n_resupply_stops(days)} planned resupply stop(s)")

# ---------------------------------------------------------------------------
# Step 7 -- Output & Reporting
# ---------------------------------------------------------------------------

print()
print("=" * 60)
print("STEP 7 -- OUTPUT & REPORTING")
print("=" * 60)
progress(93, "Output & reporting")

# Prefer open circuit; fall back to closed
itinerary_label = None
itinerary_days  = None
for lbl in ("Open", "Closed"):
    if dp_results.get(lbl) is not None:
        itinerary_label = lbl
        itinerary_days  = dp_results[lbl]
        break

if itinerary_days is None:
    print("No valid itinerary found -- detour insertion needed before reporting.")
    _env = envelope_base()
    _env.update(map_complete=False, error="no_valid_split",
                remaining_required_miles=round(float(total_req_miles), 1),
                open=None, closed=None)
    emit_json(_env)
    progress(100, "No valid split found")
    raise SystemExit

n_days = len(itinerary_days)
print(f"\nUsing {itinerary_label} circuit  ({n_days} days)\n")

# --- Day summary table (all days, one line each) ---
itinerary_cats = classify_arcs(itinerary_days)

print(f"  {'Day':>3}  {'Start':<10}  {'End':<10}  {'Time':>8}  "
      f"{'New':>8}  {'Conn':>8}  {'Repeat':>8}  {'Miles':>6}  {'+Gain':>7}")
print(f"  {'-'*3}  {'-'*10}  {'-'*10}  {'-'*8}  "
      f"{'-'*8}  {'-'*8}  {'-'*8}  {'-'*6}  {'-'*7}")

grand = {'new_s': 0, 'conn_s': 0, 'rep_s': 0, 'miles': 0.0, 'gain': 0}
campsites_used: list[str] = []

for day_num, (day_arcs, day_cats) in enumerate(zip(itinerary_days, itinerary_cats), 1):
    start_n  = day_arcs[0][0]
    end_n    = day_arcs[-1][1]
    day_s    = sum(d['weight'] for _, _, _, d in day_arcs)
    cat_s    = day_cat_seconds(day_arcs, day_cats)
    miles    = sum(d['miles']  for _, _, _, d in day_arcs)
    gain_ft  = sum(d.get('gain', 0) for _, _, _, d in day_arcs)
    grand['new_s']  += cat_s['new']
    grand['conn_s'] += cat_s['connector']
    grand['rep_s']  += cat_s['repeat']
    grand['miles']  += miles
    grand['gain']   += gain_ft
    is_last = (day_num == n_days)
    if not is_last:
        campsites_used.append(end_n)
    # Flag if a non-last day ends somewhere other than a legal overnight
    flag = " (!)" if (not G.nodes[end_n]['is_legal_overnight'] and not is_last) else ""
    print(f"  {day_num:>3}  {start_n:<10}  {end_n:<10}{flag}  "
          f"{fmt_hm(day_s):>8}  {fmt_hm(cat_s['new']):>8}  {fmt_hm(cat_s['connector']):>8}  "
          f"{fmt_hm(cat_s['repeat']):>8}  {miles:>6.1f}  {gain_ft:>7,}")

grand_total_s = grand['new_s'] + grand['conn_s'] + grand['rep_s']
print(f"\n  {'TOT':>3}  {'':10}  {'':10}  "
      f"{fmt_hm(grand_total_s):>8}  {fmt_hm(grand['new_s']):>8}  "
      f"{fmt_hm(grand['conn_s']):>8}  {fmt_hm(grand['rep_s']):>8}  "
      f"{grand['miles']:>6.1f}  {grand['gain']:>7,}")

# --- Day time statistics ---
day_times = [sum(d['weight'] for _, _, _, d in day) for day in itinerary_days]
print()
print(f"  Day time stats:")
print(f"    Min    : {fmt_hm(min(day_times))} ({min(day_times) / 3600:.2f}h)")
print(f"    Median : {fmt_hm(sorted(day_times)[n_days // 2])} "
      f"({sorted(day_times)[n_days // 2] / 3600:.2f}h)")
print(f"    Max    : {fmt_hm(max(day_times))} ({max(day_times) / 3600:.2f}h)")

# --- Grand summary ---
print()
print("=" * 60)
print("GRAND SUMMARY")
print("=" * 60)
print(f"  Direction method   : {method_4}")
print(f"  Total days         : {n_days}")
print(f"  Total miles        : {grand['miles']:.1f} mi")
print(f"  Total time         : {grand_total_s:,}s  ({grand_total_s / 3600:.1f}h)")
print(f"  New trail hiking   : {grand['new_s']:,}s  "
      f"({grand['new_s'] / 3600:.1f}h, {100 * grand['new_s'] / grand_total_s:.1f}%)")
print(f"  Connector hiking   : {grand['conn_s']:,}s  "
      f"({grand['conn_s'] / 3600:.1f}h, {100 * grand['conn_s'] / grand_total_s:.1f}%)")
print(f"  Repeat hiking      : {grand['rep_s']:,}s  "
      f"({grand['rep_s'] / 3600:.1f}h, {100 * grand['rep_s'] / grand_total_s:.1f}%)")
print(f"  Total elev gain    : {grand['gain']:,} ft")
print(f"  Distinct campsites : {len(set(campsites_used))}")

print()
print("Closed vs Open day count:")
for lbl in ("Closed", "Open"):
    days = dp_results.get(lbl)
    if days is None:
        print(f"  {lbl:8}: no valid partition")
        continue
    t    = sum(d['weight'] for day in days for _, _, _, d in day)
    cats = classify_arcs(days)
    xtra = sum(day_cat_seconds(da, dc)['connector'] + day_cat_seconds(da, dc)['repeat']
               for da, dc in zip(days, cats))
    cs = list({day[-1][1] for day in days[:-1]})
    print(f"  {lbl:8}: {len(days)} days  "
          f"({t / 3600:.1f}h total, {xtra / 3600:.1f}h connector+repeat, "
          f"{len(cs)} distinct campsites)")

# --- Constraint verification ---
print()
print("Constraint verification:")
n_over = sum(1 for t in day_times if t > MAX_DAY_SECONDS)
print(f"  Days over {MAX_DAY_SECONDS//3600}h max: {n_over}"
      + ("  OK" if n_over == 0 else "  VIOLATION"))
if n_days > 1:
    print(f"  Shortest non-last day: {fmt_hm(min(day_times[:-1]))} "
          f"(floor maximized at the optimal day count; no user minimum)")

consec_violations = 0
last_cs, consec_ct = None, 0
for day_num, day_arcs in enumerate(itinerary_days[:-1], 1):
    cs = day_arcs[-1][1]
    if cs == last_cs:
        consec_ct += 1
        if cs in single_night and consec_ct >= 1:
            consec_violations += 1
            print(f"  Day {day_num}: {cs} single-night violation (consec={consec_ct+1})")
        elif consec_ct >= 3 and cs not in town_overnights:
            consec_violations += 1
            print(f"  Day {day_num}: {cs} 3-night BC violation (consec={consec_ct+1})")
    else:
        consec_ct = 0
    last_cs = cs
print(f"  Consecutive overnight violations: {consec_violations}"
      + ("  OK" if consec_violations == 0 else ""))

# --- Resupply verification (pass-through: any touch during the day counts;
#     hiker starts supplied; final day exempt) ---
if MAX_DAYS_BETWEEN_RESUPPLY is not None:
    n_touch_days = 0
    days_since_last = 0
    resupply_violations = 0
    for day_num, day_arcs in enumerate(itinerary_days, 1):
        days_since_last += 1
        touched = {v for _u, v, _k, _d in day_arcs} & resupply_set
        if touched:
            n_touch_days += 1
            days_since_last = 0
        elif (days_since_last > MAX_DAYS_BETWEEN_RESUPPLY
              and day_num < len(itinerary_days)):
            resupply_violations += 1
            print(f"  Day {day_num}: resupply violation "
                  f"({days_since_last} days since last resupply)")
    print(f"  Resupply violations (max {MAX_DAYS_BETWEEN_RESUPPLY} days): "
          f"{resupply_violations}"
          + ("  OK" if resupply_violations == 0 else "  VIOLATION"))
    itinerary_resupply_plan = planned_resupply_schedule(
        itinerary_days, resupply_set, MAX_DAYS_BETWEEN_RESUPPLY)
    if itinerary_resupply_plan is None:
        print("  Planned resupply schedule: NONE FOUND (window violated)")
    else:
        print(f"  Planned resupply schedule: {len(itinerary_resupply_plan)} "
              f"stop(s) (starts fully supplied; walk passes a resupply "
              f"point on {n_touch_days} days)")
        for day_num, node, interval in itinerary_resupply_plan:
            park = "in park" if node in IN_PARK_RESUPPLY else "out of park"
            print(f"    Day {day_num:>3}: {RESUPPLY_NODES.get(node, node)} "
                  f"[{node}, {park}]  "
                  f"(after {interval - 1} full day(s) without resupply)")
else:
    itinerary_resupply_plan = None
    print("  Resupply constraint: disabled")

# ---------------------------------------------------------------------------
# Write full arc-level itinerary to text file
# ---------------------------------------------------------------------------
_max_h = int(_args.max_hours)
_rs_sfx = f"_r{MAX_DAYS_BETWEEN_RESUPPLY}" if MAX_DAYS_BETWEEN_RESUPPLY is not None else ""
_rs_sfx += "_town" if TOWN_NIGHTS else ""
_hk_sfx = f"_hiked{len(hiked_ids)}" if hiked_ids else ""
OUT_PATH = f'smokies_itinerary_{_max_h}h{_rs_sfx}{_hk_sfx}.txt'
with open(OUT_PATH, 'w', encoding='utf-8') as f:
    f.write(f"GSMNP Complete Trail Itinerary -- {itinerary_label} Circuit\n")
    f.write(f"{'=' * 70}\n")
    f.write(f"  Total days : {n_days}\n")
    f.write(f"  Total time : {grand_total_s / 3600:.1f}h  "
            f"({grand['new_s'] / 3600:.1f}h new trail + "
            f"{grand['conn_s'] / 3600:.1f}h connector + "
            f"{grand['rep_s'] / 3600:.1f}h repeat)\n")
    f.write(f"  Total miles: {grand['miles']:.1f} mi\n")
    f.write(f"  Elev gain  : {grand['gain']:,} ft\n\n")
    if MAX_DAYS_BETWEEN_RESUPPLY is not None and itinerary_resupply_plan:
        f.write(f"  Resupply plan (start fully supplied; never more than "
                f"{MAX_DAYS_BETWEEN_RESUPPLY} full days without a stop; "
                f"{len(itinerary_resupply_plan)} stop(s)):\n")
        for _d, _node, _iv in itinerary_resupply_plan:
            _park = "in park" if _node in IN_PARK_RESUPPLY else "out of park"
            f.write(f"    Day {_d:>3}: {RESUPPLY_NODES.get(_node, _node)} "
                    f"[{_node}, {_park}]  "
                    f"(after {_iv - 1} full day(s) without resupply)\n")
        f.write("    NOTE: out-of-park stops add town-access miles/hours "
                "not counted above.\n\n")
    f.write("  Arc tags: untagged = new trail (first pass of a required trail)\n")
    f.write("            [CN] = connector (first pass of a non-required road/path)\n")
    f.write("            [RP] = repeat (edge already traversed)\n\n")

    ARC_TAG = {'new': '    ', 'connector': '[CN]', 'repeat': '[RP]'}
    for day_num, (day_arcs, day_cats) in enumerate(zip(itinerary_days, itinerary_cats), 1):
        start_n  = day_arcs[0][0]
        end_n    = day_arcs[-1][1]
        day_s    = sum(d['weight'] for _, _, _, d in day_arcs)
        cat_s    = day_cat_seconds(day_arcs, day_cats)
        miles    = sum(d['miles']  for _, _, _, d in day_arcs)
        gain_ft  = sum(d.get('gain', 0) for _, _, _, d in day_arcs)

        f.write(f"{'=' * 70}\n")
        f.write(f"DAY {day_num:>2}  |  {start_n} -> {end_n}\n")
        f.write(f"        {fmt_hm(day_s)} total  "
                f"(new {fmt_hm(cat_s['new'])}, conn {fmt_hm(cat_s['connector'])}, "
                f"repeat {fmt_hm(cat_s['repeat'])})  "
                f"{miles:.1f} mi  +{gain_ft:,} ft\n")
        f.write(f"{'-' * 70}\n")

        for (u, v, k, d), cat in zip(day_arcs, day_cats):
            tag = ARC_TAG[cat]
            if "_fwd" in k:
                dir_flag = "A->B"
            elif "_rev" in k:
                dir_flag = "B->A"
            else:
                dir_flag = " -- "
            trail   = d.get('trail', '')
            arc_min = d['weight'] // 60
            arc_mi  = d['miles']
            f.write(f"  {tag} {dir_flag}  {u:<8} -> {v:<8}  "
                    f"{arc_min:>4}min  {arc_mi:>5.2f}mi  {trail}\n")
        f.write("\n")

print(f"\nFull arc-level itinerary written to: {OUT_PATH}")

# ---------------------------------------------------------------------------
# Export preset JSONs for both circuits to docs/data/
# ---------------------------------------------------------------------------
import json as _json

total_req_miles = float(df.loc[df['is_required'].astype(bool), 'Miles'].sum())

def _build_preset(label, days):
    export = {
        "circuit": label,
        "n_days": len(days),
        "total_required_miles": total_req_miles,
        "days": [],
    }
    if MAX_DAYS_BETWEEN_RESUPPLY is not None:
        _sched = planned_resupply_schedule(days, resupply_set,
                                           MAX_DAYS_BETWEEN_RESUPPLY)
        export["max_days_between_resupply"] = MAX_DAYS_BETWEEN_RESUPPLY
        export["resupply_plan"] = [
            {"day": _d, "node": _node,
             "name": RESUPPLY_NODES.get(_node, _node),
             "in_park": _node in IN_PARK_RESUPPLY,
             "days_since_last": _iv}
            for _d, _node, _iv in (_sched or [])
        ]
    for day_num, day_arcs in enumerate(days, 1):
        day_dict = {
            "day": day_num,
            "start_node": day_arcs[0][0],
            "end_node": day_arcs[-1][1],
            "arcs": [],
        }
        for u, v, k, d in day_arcs:
            day_dict["arcs"].append({
                "from": u,
                "to": v,
                "edge_id": d.get("edge_id"),
                "trail": d.get("trail", ""),
                "miles": d.get("miles", 0.0),
                "seconds": d.get("weight", 0),
                "gain": d.get("gain", 0),
                "is_deadhead": bool(d.get("is_deadhead", False)),
                "direction": ("fwd" if "_fwd" in k else ("rev" if "_rev" in k else "dh")),
            })
        export["days"].append(day_dict)
    return export

_preset_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'docs', 'data')
os.makedirs(_preset_dir, exist_ok=True)

print()
print("=" * 60)
print("PRESET JSON EXPORT")
print("=" * 60)
if hiked_ids:
    # Custom partial-completion runs must never overwrite published presets.
    print("  Skipped: --hiked run (presets cover the full map only)")
else:
    for _lbl in ("Open", "Closed"):
        _days = dp_results.get(_lbl)
        if _days is None:
            print(f"  {_lbl}: no valid partition — skipped")
            continue
        _export = _build_preset(_lbl, _days)
        _fname  = f"preset_{_lbl.lower()}_{_max_h}h{_rs_sfx}.json"
        _path   = os.path.join(_preset_dir, _fname)
        with open(_path, "w", encoding="utf-8") as _jf:
            _json.dump(_export, _jf, indent=2)
        print(f"  {_lbl:6}: {len(_days)} days  -> {_path}")

# ---------------------------------------------------------------------------
# JSON result envelope (--json-out): same day/arc schema as the presets, so
# the web app's loadPreset() path can render a custom solution unchanged.
# ---------------------------------------------------------------------------
if _args.json_out:
    _env = envelope_base()
    _env.update(
        map_complete=False,
        remaining_required_miles=round(float(total_req_miles), 1),
        open=(_build_preset("Open", dp_results["Open"])
              if dp_results.get("Open") else None),
        closed=(_build_preset("Closed", dp_results["Closed"])
                if dp_results.get("Closed") else None),
    )
    emit_json(_env)
progress(100, "Done")
