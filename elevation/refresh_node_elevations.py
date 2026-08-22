"""Re-derive node elevations from the 1 m tiles.

The elevations published in points_20250211.geojson (and copied into elev_A /
elev_B in the edge list) are a February 2025 re-derivation that regressed: at
coordinates that did not move, they sit a median of 4.0 ft from a direct 1 m
3DEP sample, p90 12.3 ft, worst 29 ft, with only 21% inside 1.5 ft.  The
January 2025 values were 0.66 ft / 81% -- essentially the rounding floor.

Nothing downstream computes a time from these.  The solver reads
cost_A_to_B for edge weights and carries elev_start / elev_end only as
reporting attributes; viz.js shows them and uses them as a fallback when an
edge is missing from edge_gains.  So this is a display-data fix: no itinerary
changes, no re-solve.

    python -m elevation.refresh_node_elevations            # report only
    python -m elevation.refresh_node_elevations --write    # apply
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys

from .forensics import load_nodes
from .mosaic import Mosaic
from .tiles import REPO

POINT_FILES = [
    os.path.join(REPO, "docs", "data", "points_20250211.geojson"),
    os.path.join(REPO, "smokies", "docs", "data", "points_20250211.geojson"),
    os.path.join(REPO, "smokies", "points_20250211.geojson"),
    os.path.join(REPO, "points_20250211.geojson"),
]
EDGES = [
    os.path.join(REPO, "smokies_edge_list_20260509a.csv"),
    os.path.join(REPO, "smokies", "solver", "smokies_edge_list_20260509a.csv"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    nodes = load_nodes()
    with Mosaic() as mo:
        vals = mo.sample([(n["easting"], n["northing"]) for n in nodes])

    new: dict[str, int] = {}
    deltas = []
    for n, v in zip(nodes, vals):
        if v is None:
            continue
        new[n["id"]] = int(round(v))
        deltas.append(new[n["id"]] - n["stored_ft"])
    deltas.sort(key=abs)
    print(f"nodes resampled : {len(new)} of {len(nodes)}")
    print(f"change          : median |d| {statistics.median([abs(d) for d in deltas]):.0f} ft, "
          f"mean {statistics.mean(deltas):+.1f} ft, "
          f"largest {deltas[-1]:+.0f} ft")
    print(f"unchanged       : {sum(1 for d in deltas if d == 0)} node(s)")
    print()
    print("biggest corrections:")
    for n in sorted(nodes, key=lambda n: -abs(new.get(n["id"], n["stored_ft"]) - n["stored_ft"]))[:8]:
        if n["id"] in new:
            print(f"  {n['id']:<7} {n['name'][:34]:<36} {n['stored_ft']:6.0f} -> {new[n['id']]:6.0f} ft")

    if not args.write:
        print()
        print("dry run -- pass --write to apply")
        return 0

    for p in POINT_FILES:
        if not os.path.exists(p):
            continue
        gj = json.load(open(p, encoding="utf-8"))
        n = 0
        for f in gj["features"]:
            nid = f["properties"].get("id")
            if nid in new:
                f["properties"]["elevation"] = str(new[nid])
                n += 1
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(gj, fh, separators=(",", ":"))
        print(f"  points updated ({n}): {p}")

    for p in EDGES:
        if not os.path.exists(p):
            continue
        rows = list(csv.DictReader(open(p, encoding="utf-8-sig")))
        fields = list(rows[0])
        n = 0
        for r in rows:
            if r["node_A"] in new:
                r["elev_A"] = str(new[r["node_A"]]); n += 1
            if r["node_B"] in new:
                r["elev_B"] = str(new[r["node_B"]])
        with open(p, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
            w.writeheader(); w.writerows(rows)
        print(f"  edge list updated ({n} rows): {p}")
    print()
    print("costs and gains untouched -- no re-solve needed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
