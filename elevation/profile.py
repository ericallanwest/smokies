"""Step 6 -- elevation profiles along the network, and the two knobs that set gain.

Sampling a point elevation is settled: the 1 m tiles reproduce the good January
2025 node values to within the rounding floor.  Gain is not settled, because it
is not a measurement -- it is an integral, and its value depends on how finely
you walk the line and how much wiggle you refuse to count:

  * spacing -- vertices are irregular (metres apart in places, tens elsewhere).
    Integrating raw 1 m LiDAR at every vertex accumulates sensor noise, canopy
    penetration error and the line's own wander into "gain" nobody climbs.
  * threshold -- ignoring rises smaller than some amount suppresses that noise,
    at the cost of flattening genuinely rolling ground if set too high.

Neither has a correct value derivable from first principles, so this module
measures them: it sweeps both, and scores each combination against the gains
already published (which came from a per-vertex integration in a spreadsheet).

Tobler is integrated per step, never once per segment.  The function is strongly
non-linear in slope, so 500 ft of gain over 5 miles and 500 ft over half a mile
are different walks; collapsing a segment to one average slope loses exactly the
thing the model is for.

    python -m elevation.profile [--limit N] [--csv out.csv]
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import sys

from pyproj import Transformer

from .mosaic import Mosaic
from .tiles import REPO

LINES = os.path.join(REPO, "docs", "data", "lines_20250211.geojson")
EDGES = os.path.join(REPO, "smokies_edge_list_20260509a.csv")

FT_M = 0.3048
_to_utm = Transformer.from_crs("EPSG:4326", "EPSG:26917", always_xy=True)

# Tobler's hiking function, metres per hour, slope as rise/run.
TOBLER_V0 = 6000.0
TOBLER_K = 3.5
TOBLER_PEAK = -0.05


def tobler_mph(slope: float) -> float:
    return TOBLER_V0 * math.exp(-TOBLER_K * abs(slope - TOBLER_PEAK))


def densify(pts_xy, spacing_m: float):
    """Resample a polyline to (roughly) fixed spacing, keeping both ends."""
    if spacing_m <= 0 or len(pts_xy) < 2:
        return list(pts_xy)
    out = [pts_xy[0]]
    carry = 0.0
    for (x0, y0), (x1, y1) in zip(pts_xy, pts_xy[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg == 0:
            continue
        t = spacing_m - carry
        while t < seg:
            f = t / seg
            out.append((x0 + (x1 - x0) * f, y0 + (y1 - y0) * f))
            t += spacing_m
        carry = (carry + seg) % spacing_m
    if out[-1] != pts_xy[-1]:
        out.append(pts_xy[-1])
    return out


# Steps steeper than this are not walking, they are the DEM disagreeing with the
# trail: bare earth reports the top of a bluff or the roof of an arch where the
# tread goes through or under it.  Alum Cave is the worst case -- ten steps near
# Arch Rock read 130% to 260% and, left uncapped, produce 90% of that segment's
# entire hiking time.  Because Tobler is convex in slope, a handful of artifacts
# can outweigh five miles of real trail; because gain is linear, they barely show
# up in the gain total, so the fault hides until you look at time.
#
# 0.60 is chosen from the data: only 0.145% of the network's 157,083 steps exceed
# 50%, capping anywhere in 50-100% leaves the median reproduction untouched
# (0.37% -> 0.38%), and 60% gives the best worst case.  This is the road-and-
# bridge repair generalised -- same fault, and trails need it more than roads do.
SLOPE_CAP = 0.60


def integrate(pts_xy, elev_ft, threshold_ft: float = 0.0,
              slope_cap: float | None = SLOPE_CAP):
    """Gain, loss and both Tobler times for one sampled profile.

    The threshold is applied as hysteresis against the last *accepted*
    elevation, not between neighbours: that way a long steady climb sampled in
    small steps still counts in full, while noise oscillating below the
    threshold never accumulates.

    The cap applies to the slope handed to Tobler, not to the rise counted as
    gain -- the ground really does go up there, it just cannot be walked at the
    gradient the DEM claims.
    """
    gain = loss = 0.0
    t_fwd = t_rev = 0.0
    ref = elev_ft[0]
    for i in range(1, len(pts_xy)):
        (x0, y0), (x1, y1) = pts_xy[i - 1], pts_xy[i]
        run = math.hypot(x1 - x0, y1 - y0)
        if run <= 0:
            continue
        if elev_ft[i] is None or ref is None:
            continue
        rise_ft = elev_ft[i] - ref
        if abs(rise_ft) < threshold_ft:
            rise_used = 0.0
        else:
            rise_used = rise_ft
            ref = elev_ft[i]
        rise_m = rise_used * FT_M
        slope = rise_m / run
        if slope_cap is not None:
            slope = max(-slope_cap, min(slope_cap, slope))
        if rise_used > 0:
            gain += rise_used
        else:
            loss += -rise_used
        t_fwd += run / tobler_mph(slope) * 3600.0
        t_rev += run / tobler_mph(-slope) * 3600.0
    return gain, loss, t_fwd, t_rev


def load_segments():
    gj = json.load(open(LINES, encoding="utf-8"))
    edges = {str(float(r["ID"])): r
             for r in csv.DictReader(open(EDGES, encoding="utf-8-sig"))}
    segs = []
    for f in gj["features"]:
        p = f["properties"]
        sid = str(float(p["Segment"]))
        g = f["geometry"]
        parts = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
        ll = [c for part in parts for c in part]
        xy = [_to_utm.transform(c[0], c[1]) for c in ll]
        segs.append({"id": sid, "name": p["Name"], "xy": xy, "edge": edges.get(sid)})
    return segs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="only the first N segments")
    ap.add_argument("--csv", help="write per-segment results for the best setting")
    args = ap.parse_args()

    segs = load_segments()
    if args.limit:
        segs = segs[:args.limit]
    total_v = sum(len(s["xy"]) for s in segs)
    print(f"segments {len(segs)}, native vertices {total_v:,}")

    SPACINGS = [0, 5, 10, 20, 30]
    THRESHOLDS = [0.0, 1.0, 2.0, 3.0, 5.0]

    with Mosaic() as mo:
        # Sample once per spacing; thresholds are free after that.
        sampled = {}
        for sp in SPACINGS:
            pts = {}
            allpts = []
            for s in segs:
                d = densify(s["xy"], sp)
                pts[s["id"]] = (len(allpts), len(d))
                allpts.extend(d)
            vals = mo.sample(allpts)
            miss = sum(1 for v in vals if v is None)
            sampled[sp] = (pts, allpts, vals)
            print(f"  spacing {('native' if sp == 0 else str(sp) + ' m'):>7}: "
                  f"{len(allpts):>8,} samples, {miss} uncovered")

        print()
        print("gain reproduced against the published gain_A_to_B "
              "(the spreadsheet's per-vertex integration):")
        print(f"  {'spacing':>8}{'thresh':>8}{'median ratio':>14}{'median |err|':>14}"
              f"{'within 10%':>12}")
        best = None
        results = {}
        for sp in SPACINGS:
            pts, allpts, vals = sampled[sp]
            for th in THRESHOLDS:
                ratios, errs = [], []
                per = {}
                for s in segs:
                    if not s["edge"]:
                        continue
                    off, n = pts[s["id"]]
                    xy = allpts[off:off + n]
                    ev = vals[off:off + n]
                    if any(v is None for v in ev) or n < 2:
                        continue
                    gain, loss, tf, tr = integrate(xy, ev, th)
                    per[s["id"]] = (gain, loss, tf, tr)
                    ref = int(s["edge"]["gain_A_to_B"])
                    if ref >= 50:
                        ratios.append(gain / ref)
                        errs.append(abs(gain - ref) / ref)
                if not ratios:
                    continue
                results[(sp, th)] = per
                errs.sort()
                med = statistics.median(ratios)
                w10 = sum(1 for e in errs if e <= .10)
                print(f"  {('native' if sp == 0 else str(sp) + 'm'):>8}{th:>7.0f}ft"
                      f"{med:>14.3f}{100 * statistics.median(errs):>13.1f}%"
                      f"{w10:>8} /{len(errs):>4}")
                score = abs(med - 1.0) + statistics.median(errs)
                if best is None or score < best[0]:
                    best = (score, sp, th)

    _score, sp, th = best
    print()
    print(f"closest reproduction: spacing "
          f"{'native vertices' if sp == 0 else str(sp) + ' m'}, "
          f"threshold {th:.0f} ft")

    per = results[(sp, th)]
    tf_err = []
    for s in segs:
        if s["id"] in per and s["edge"]:
            obs = int(s["edge"]["cost_A_to_B"])
            if obs > 0:
                tf_err.append(abs(per[s["id"]][2] - obs) / obs)
    if tf_err:
        tf_err.sort()
        print(f"  Tobler time vs published cost_A_to_B: median "
              f"{100 * statistics.median(tf_err):.1f}% error, "
              f"p90 {100 * tf_err[int(.9 * len(tf_err))]:.1f}%, "
              f"{sum(1 for e in tf_err if e <= .05)}/{len(tf_err)} within 5%")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["edge_id", "trail", "gain_new", "loss_new",
                        "seconds_A_to_B", "seconds_B_to_A",
                        "gain_published", "cost_A_to_B_published"])
            for s in segs:
                if s["id"] not in per or not s["edge"]:
                    continue
                g, l, tfw, trv = per[s["id"]]
                w.writerow([s["id"], s["name"], round(g), round(l),
                            round(tfw), round(trv),
                            s["edge"]["gain_A_to_B"], s["edge"]["cost_A_to_B"]])
        print(f"  per-segment results written to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
