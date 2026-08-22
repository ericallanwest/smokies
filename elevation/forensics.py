"""Step 1 -- recover the tie rule the original hand-built elevations used.

35 of the 87 tiles the park needs are delivered by two or three separate USGS
acquisitions, and nothing on disk records which one was picked where.  But the
answer is latent in the numbers themselves: sample every competing project at
the 383 node coordinates and compare against the stored elevations.  Whichever
project reproduces the stored integers on a contested tile is the one the
original process used there.

The stored values are integer feet, so the test resolves to about 0.3 m -- fine
for separating acquisitions that typically differ by more, and honest about the
cases where two projects agree too closely to tell apart.  Those are reported as
undecidable rather than guessed.

Resampling method is recovered the same way: nearest and bilinear are both
sampled, and whichever tracks the stored values better says how the original
point query was configured.

    python -m elevation.forensics [--csv out.csv]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import sys

from .sample import lonlat_to_utm, sample_tile, tile_bounds
from .tiles import REPO, build_index, by_tile_index, tile_index_for

POINTS = os.path.join(REPO, "docs", "data", "points_20250211.geojson")

# A project is only credited with a node when it lands this close, and two
# projects are only distinguishable when they differ by more than this.
MATCH_FT = 1.5


def load_nodes():
    gj = json.load(open(POINTS, encoding="utf-8"))
    nodes = []
    for f in gj["features"]:
        p = f["properties"]
        if p.get("elevation") in (None, ""):
            continue
        e, n = lonlat_to_utm(float(p["longitude"]), float(p["latitude"]))
        nodes.append({
            "id": p["id"], "name": p.get("name", ""),
            "easting": e, "northing": n,
            "stored_ft": float(p["elevation"]),
            "tile": tile_index_for(e, n),
        })
    return nodes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="write the per-node sample table here")
    args = ap.parse_args()

    nodes = load_nodes()
    index = build_index()
    groups = by_tile_index(index)
    print(f"nodes with a stored elevation : {len(nodes)}")
    print(f"tiles indexed                 : {len(index)} "
          f"over {len(groups)} tile indices")

    # Sample every tile once, at whichever nodes fall inside it.  Tiles carry a
    # 6 m collar, so a node near a tile edge is sampled from both neighbours;
    # that is a feature -- it shows up as agreement, not as a conflict.
    samples: dict[tuple[str, str], dict[str, float]] = {}   # (node, project) -> {method: ft}
    skipped_remote = 0
    for t in sorted(index.values(), key=lambda t: (t.project, t.x, t.y)):
        if not t.local:
            skipped_remote += 1
            continue
        left, bottom, right, top = tile_bounds(t.path)
        here = [n for n in nodes
                if left <= n["easting"] <= right and bottom <= n["northing"] <= top]
        if not here:
            continue
        pts = [(n["easting"], n["northing"]) for n in here]
        for method in ("nearest", "bilinear"):
            vals = sample_tile(t.path, pts, method=method)
            for n, v in zip(here, vals):
                if v is not None:
                    samples.setdefault((n["id"], t.project), {})[method] = v
    if skipped_remote:
        print(f"  ({skipped_remote} tile(s) not downloaded were skipped)")

    # ---- which resampling method reproduces the stored numbers? ------------
    print()
    print("=" * 68)
    print("RESAMPLING METHOD")
    print("=" * 68)
    for method in ("nearest", "bilinear"):
        diffs = []
        for n in nodes:
            best = None
            for (nid, _proj), m in samples.items():
                if nid == n["id"] and method in m:
                    d = abs(m[method] - n["stored_ft"])
                    best = d if best is None else min(best, d)
            if best is not None:
                diffs.append(best)
        if not diffs:
            continue
        diffs.sort()
        within = sum(1 for d in diffs if d <= MATCH_FT)
        print(f"  {method:<9} best-project |difference| vs stored: "
              f"median {statistics.median(diffs):5.2f} ft, "
              f"p90 {diffs[int(.9 * len(diffs))]:6.2f} ft, "
              f"{within}/{len(diffs)} within {MATCH_FT} ft")

    # ---- which project was used on each contested tile? -------------------
    print()
    print("=" * 68)
    print("TIE RULE ON CONTESTED TILES")
    print("=" * 68)
    verdicts: dict[tuple[int, int], collections.Counter] = {}
    undecidable: dict[tuple[int, int], int] = collections.Counter()
    contested = {k: v for k, v in groups.items() if len(v) > 1}

    for n in nodes:
        cand = {p: m["bilinear"] for (nid, p), m in samples.items()
                if nid == n["id"] and "bilinear" in m}
        if len(cand) < 2:
            continue
        ti = n["tile"]
        ranked = sorted(cand.items(), key=lambda kv: abs(kv[1] - n["stored_ft"]))
        (best_p, best_v), (_, second_v) = ranked[0], ranked[1]
        # If the two closest projects agree with each other, the node cannot
        # tell them apart -- say so instead of crediting the winner.
        if abs(best_v - second_v) <= MATCH_FT:
            undecidable[ti] += 1
        elif abs(best_v - n["stored_ft"]) <= MATCH_FT:
            verdicts.setdefault(ti, collections.Counter())[best_p] += 1
        else:
            verdicts.setdefault(ti, collections.Counter())["(no project matches)"] += 1

    n_nodes_contested = sum(
        1 for n in nodes
        if len({p for (nid, p) in samples if nid == n["id"]}) > 1)
    print(f"contested tiles              : {len(contested)}")
    print(f"nodes sitting on one         : {n_nodes_contested}")
    print(f"nodes that can discriminate  : "
          f"{sum(sum(c.values()) for c in verdicts.values())}")
    print(f"nodes where projects agree   : {sum(undecidable.values())} "
          f"(within {MATCH_FT} ft of each other -- no evidence either way)")
    if verdicts:
        print()
        print(f"{'tile':<10} {'verdict':<34} {'nodes'}")
        for ti in sorted(verdicts):
            for proj, cnt in verdicts[ti].most_common():
                print(f"  x{ti[0]}y{ti[1]:<5} {proj:<34} {cnt}")

    overall = collections.Counter()
    for c in verdicts.values():
        overall.update(c)
    if overall:
        print()
        print("across all contested tiles, the stored elevations came from:")
        for proj, cnt in overall.most_common():
            print(f"  {cnt:4d} node(s)  {proj}")

    # ---- how far apart are the projects where they overlap? ---------------
    print()
    print("=" * 68)
    print("SEAM MAGNITUDE  (how much the tie rule can matter)")
    print("=" * 68)
    spreads = []
    for n in nodes:
        cand = [m["bilinear"] for (nid, _p), m in samples.items()
                if nid == n["id"] and "bilinear" in m]
        if len(cand) >= 2:
            spreads.append((max(cand) - min(cand), n))
    if spreads:
        vals = sorted(s for s, _ in spreads)
        print(f"  nodes covered by 2+ projects : {len(vals)}")
        print(f"  disagreement between them    : median {statistics.median(vals):.2f} ft, "
              f"p90 {vals[int(.9 * len(vals))]:.2f} ft, max {vals[-1]:.2f} ft")
        print()
        print("  widest disagreements:")
        for s, n in sorted(spreads, key=lambda t: -t[0])[:8]:
            cand = {p: m["bilinear"] for (nid, p), m in samples.items()
                    if nid == n["id"] and "bilinear" in m}
            detail = ", ".join(f"{p.split('_')[0]}..{p[-4:]}={v:.1f}"
                               for p, v in sorted(cand.items()))
            print(f"    {s:6.1f} ft  {n['id']} {n['name'][:26]:<28} "
                  f"stored {n['stored_ft']:.0f} | {detail}")

    if args.csv:
        import csv as _csv
        projects = sorted({p for _n, p in samples})
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = _csv.writer(f)
            w.writerow(["node", "name", "stored_ft", "tile_x", "tile_y"] +
                       [f"{p}_ft" for p in projects])
            for n in nodes:
                row = [n["id"], n["name"], n["stored_ft"], n["tile"][0], n["tile"][1]]
                for p in projects:
                    m = samples.get((n["id"], p))
                    row.append(round(m["bilinear"], 2) if m and "bilinear" in m else "")
                w.writerow(row)
        print()
        print(f"per-node table written to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
