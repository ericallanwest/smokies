"""Step 7 -- build the per-segment elevation product the app needs.

Emits one file, `docs/data/segment_profiles.json`, holding for every segment:

  * gain and loss, in feet, from the uncapped rises -- the ground really does
    go up there;
  * a slope histogram in 1% bins, each bin carrying its own distance and rise,
    so a time can be recomputed at that bin's actual mean slope rather than at
    a nominal bin centre.  Bin width then barely affects accuracy;
  * the seconds each way under the default Tobler parameters, for checking.

One histogram per segment, not two.  Reversing a traversal negates every slope
over the same distances, so the return direction is the same histogram mirrored
about zero -- half the data, and the two directions cannot disagree.

This is what makes user-adjustable Tobler parameters possible without shipping a
DEM to the browser: elevations stay pre-applied and committed, while the times
stay re-derivable from three numbers.  A single gain scalar would have satisfied
"pre-applied" and quietly made that impossible.

    python -m elevation.build [--out PATH] [--write-edges]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
import sys
from collections import defaultdict

from .mosaic import Mosaic, PRIORITY
from .profile import FT_M, SLOPE_CAP, load_segments
from .tiles import REPO
from .tobler import DEFAULT as TOBLER

OUT_DEFAULT = os.path.join(REPO, "docs", "data", "segment_profiles.json")
EDGES = os.path.join(REPO, "smokies_edge_list_20260509a.csv")

BIN_WIDTH = 0.01           # 1% slope bins


def bin_index(slope: float) -> int:
    """Bin a slope, by its floor, so bin b covers [b*w, (b+1)*w)."""
    return int(math.floor(slope / BIN_WIDTH))


def build_segment(xy, elev_ft):
    """Histogram, gain and loss for one sampled segment."""
    bins: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    gain = loss = 0.0
    steepest = 0.0
    capped_m = 0.0
    for i in range(1, len(xy)):
        (x0, y0), (x1, y1) = xy[i - 1], xy[i]
        run = math.hypot(x1 - x0, y1 - y0)
        if run <= 0 or elev_ft[i] is None or elev_ft[i - 1] is None:
            continue
        rise_ft = elev_ft[i] - elev_ft[i - 1]
        if rise_ft > 0:
            gain += rise_ft
        else:
            loss += -rise_ft
        raw = rise_ft * FT_M / run
        steepest = max(steepest, abs(raw))
        slope = max(-SLOPE_CAP, min(SLOPE_CAP, raw))
        if slope != raw:
            capped_m += run
        b = bin_index(slope)
        bins[b][0] += run
        bins[b][1] += slope * run          # rise implied by the capped slope
    return bins, gain, loss, steepest, capped_m


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--write-edges", action="store_true",
                    help="also rewrite gain/cost columns in the edge list")
    args = ap.parse_args()

    segs = load_segments()
    published = {str(float(r["ID"])): r
                 for r in csv.DictReader(open(EDGES, encoding="utf-8-sig"))}

    allpts, idx = [], {}
    for s in segs:
        idx[s["id"]] = (len(allpts), len(s["xy"]))
        allpts.extend(s["xy"])
    print(f"segments {len(segs)}, vertices {len(allpts):,}")

    with Mosaic() as mo:
        elev = mo.sample(allpts)
    missing = sum(1 for v in elev if v is None)
    print(f"sampled, {missing} vertex/vertices uncovered")

    out_segments = {}
    t_err, g_err = [], []
    n_capped = 0
    total_capped_m = 0.0
    for s in segs:
        off, n = idx[s["id"]]
        xy, ev = s["xy"], elev[off:off + n]
        if n < 2:
            continue
        bins, gain, loss, steepest, capped_m = build_segment(xy, ev)
        if not bins:
            continue
        if capped_m > 0:
            n_capped += 1
            total_capped_m += capped_m

        packed = {str(b): [round(d, 2), round(r, 3)] for b, (d, r) in sorted(bins.items())}
        t_fwd = TOBLER.time_from_histogram(bins)
        t_rev = TOBLER.time_from_histogram(bins, reverse=True)

        rec = {
            "trail": s["name"],
            "length_m": round(sum(d for d, _r in bins.values()), 1),
            "gain_ft": round(gain),
            "loss_ft": round(loss),
            # One decimal, not integer: on a 90 s connector, rounding to whole
            # seconds is 0.5% -- enough to look like a compression error when
            # it is only display precision.  The bins remain authoritative.
            "seconds_fwd": round(t_fwd, 1),
            "seconds_rev": round(t_rev, 1),
            "steepest_raw_slope": round(steepest, 3),
            "bins": packed,
        }
        p = published.get(s["id"])
        if p:
            rec["nodes"] = [p["node_A"], p["node_B"]]
            if int(p["cost_A_to_B"]) > 0:
                t_err.append(abs(t_fwd - int(p["cost_A_to_B"])) / int(p["cost_A_to_B"]))
            if int(p["gain_A_to_B"]) >= 50:
                g_err.append(abs(gain - int(p["gain_A_to_B"])) / int(p["gain_A_to_B"]))
        out_segments[s["id"]] = rec

    # --- self-check: the histogram as PUBLISHED must reproduce direct
    #     integration.  Compare against the unrounded integral and rebuild the
    #     time from the rounded bins actually written to the file, so the check
    #     measures the compression, not the display rounding on seconds_fwd.
    from .profile import integrate
    hist_err = []
    for s in segs:
        off, n = idx[s["id"]]
        if s["id"] not in out_segments or n < 2:
            continue
        _g, _l, tf, _tr = integrate(s["xy"], elev[off:off + n])
        shipped = {int(b): v for b, v in out_segments[s["id"]]["bins"].items()}
        got = TOBLER.time_from_histogram(shipped)
        if tf > 0:
            hist_err.append(abs(got - tf) / tf)

    payload = {
        "generated_by": "elevation/build.py",
        "provenance": {
            "dem": "USGS 3DEP 1 m, NAD83 / UTM 17N (EPSG:26917), NAVD88 metres",
            "tile_priority": PRIORITY,
            "tile_manifest": "smokies/gsmnp_1m_elevation_files.csv",
            "manifest_sha256": hashlib.sha256(
                open(os.path.join(REPO, "smokies", "gsmnp_1m_elevation_files.csv"),
                     "rb").read()).hexdigest()[:16],
            "geometry": "docs/data/lines_20250211.geojson (native vertices, no densification)",
            "sampling": "bilinear",
            "gain_threshold_ft": 0.0,
            "slope_cap": SLOPE_CAP,
            "units": "distance metres, rise metres, gain/loss feet, time seconds",
        },
        "tobler_defaults": {"v0_m_per_h": TOBLER.v0, "k": TOBLER.k, "peak_slope": TOBLER.peak},
        "bin_width": BIN_WIDTH,
        "bin_semantics": ("bins map floor(slope / bin_width) -> [distance_m, rise_m]; "
                          "time = sum(d / tobler(rise/d)); reverse = negate every slope"),
        "segments": out_segments,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    size = os.path.getsize(args.out)

    occupied = [len(r["bins"]) for r in out_segments.values()]
    print()
    print(f"segments written      : {len(out_segments)}")
    print(f"occupied bins/segment : median {statistics.median(occupied):.0f}, "
          f"max {max(occupied)} (dense +/-60% would be 121)")
    print(f"slope range in use    : {min(int(b) for r in out_segments.values() for b in r['bins'])}"
          f" to {max(int(b) for r in out_segments.values() for b in r['bins'])} "
          f"(x {BIN_WIDTH:.0%})")
    print(f"segments touched by the cap: {n_capped} "
          f"({total_capped_m:.0f} m of {sum(r['length_m'] for r in out_segments.values()):,.0f} m)")
    print(f"file                  : {args.out}  ({size / 1024:.0f} KB)")
    print()
    if hist_err:
        hist_err.sort()
        print(f"histogram vs direct integration : max {100 * hist_err[-1]:.4f}% "
              f"over {len(hist_err)} segments")
    for label, e in (("gain vs published", g_err), ("time vs published", t_err)):
        if e:
            e.sort()
            print(f"{label:<32}: median {100 * statistics.median(e):.2f}%, "
                  f"p90 {100 * e[int(.9 * len(e))]:.2f}%, "
                  f"worst {100 * e[-1]:.1f}%")

    if args.write_edges:
        rows = list(csv.DictReader(open(EDGES, encoding="utf-8-sig")))
        fields = list(rows[0])
        n = 0
        for r in rows:
            rec = out_segments.get(str(float(r["ID"])))
            if not rec:
                continue
            r["gain_A_to_B"] = str(rec["gain_ft"])
            r["gain_B_to_A"] = str(rec["loss_ft"])
            r["cost_A_to_B"] = str(rec["seconds_fwd"])
            r["cost_B_to_A"] = str(rec["seconds_rev"])
            n += 1
        with open(EDGES, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        print(f"\nedge list rewritten: {n} rows -- re-run the solver to refresh presets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
