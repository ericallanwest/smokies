"""Was the old elevation source coarser, or were the coordinates off?

`diagnose` shows the stored elevations disagreeing with a 1 m 3DEP sample by an
amount that grows with terrain slope, at zero net bias.  Two very different
causes produce that same signature, and they are easy to confuse:

  * the coordinates are off the feature by some distance -- sampling a steep
    hillside a few metres away gives an unbiased but slope-scaled error;
  * the elevations came from a COARSER DEM -- a 10 m cell reports the average of
    the ground across 10 m, which on a steep slope differs from the 1 m value by
    an amount that also scales with slope, also without bias.

Averaging the 1 m surface over a window of half-width w mimics a DEM of cell
size ~2w.  So sweep w and see which one reproduces the stored numbers:

  * if some w drives the residual down to the quantization floor -- the stored
    values are integer feet, so roughly 0.3 ft -- the old source was a DEM of
    about that cell size and the coordinates are fine;
  * if the residual bottoms out well above the floor and stays there, smoothing
    cannot explain the disagreement and a horizontal offset remains the better
    explanation.

    python -m elevation.probe_resolution
"""
from __future__ import annotations

import math
import statistics
import sys

import numpy as np
import rasterio
from rasterio.windows import Window

from .forensics import load_nodes
from .sample import M_TO_FT, tile_bounds
from .tiles import NODATA, build_index

# Half-widths in metres == 1 m cells.  0 means the single cell under the point.
HALF_WIDTHS = [0, 2, 5, 7, 10, 15, 20, 30, 45]
MAX_W = max(HALF_WIDTHS)


def window_means(path: str, nodes):
    """Mean 1 m elevation (ft) around each node, for every half-width."""
    out: dict[str, dict[int, float]] = {}
    with rasterio.open(path) as ds:
        inv = ~ds.transform
        for n in nodes:
            col_f, row_f = inv * (n["easting"], n["northing"])
            r, c = int(row_f), int(col_f)
            r0, c0 = r - MAX_W, c - MAX_W
            side = 2 * MAX_W + 1
            if r0 < 0 or c0 < 0 or r0 + side > ds.height or c0 + side > ds.width:
                continue                      # too close to the tile edge
            blk = ds.read(1, window=Window(c0, r0, side, side)).astype(float)
            # Tiles are not consistent about their sentinel: some declare
            # -999999, others the Float32 minimum.  Anything implausibly low
            # is NoData, and one such cell disqualifies the whole window.
            blk[blk < -1000] = np.nan
            if ds.nodata is not None:
                blk[blk == ds.nodata] = np.nan
            if not np.isfinite(blk).all():
                continue
            mid = MAX_W
            per = {}
            for w in HALF_WIDTHS:
                sub = blk[mid - w:mid + w + 1, mid - w:mid + w + 1]
                per[w] = float(sub.mean()) * M_TO_FT
            out[n["id"]] = per
    return out


def main() -> int:
    nodes = load_nodes()
    stored = {n["id"]: n["stored_ft"] for n in nodes}

    means: dict[str, dict[int, float]] = {}
    for t in build_index().values():
        if not t.local:
            continue
        left, bottom, right, top = tile_bounds(t.path)
        here = [n for n in nodes
                if left <= n["easting"] <= right and bottom <= n["northing"] <= top
                and n["id"] not in means]
        if not here:
            continue
        means.update(window_means(t.path, here))

    if not means:
        print("no nodes could be sampled")
        return 1

    print(f"nodes measured : {len(means)} of {len(nodes)}")
    print()
    print("Averaging the 1 m surface over a window mimics a coarser DEM.")
    print("If the old source was coarser, one of these rows should collapse")
    print("toward the ~0.3 ft floor that integer-foot storage imposes.")
    print()
    print(f"  {'window':<12}{'~cell size':<13}{'median |diff|':>15}{'mean diff':>12}"
          f"{'within 1.5 ft':>15}")
    best = None
    for w in HALF_WIDTHS:
        diffs = [means[i][w] - stored[i] for i in means]
        med = statistics.median(abs(d) for d in diffs)
        within = sum(1 for d in diffs if abs(d) <= 1.5)
        label = "point" if w == 0 else f"+/- {w} m"
        print(f"  {label:<12}{(1 if w == 0 else 2 * w):>4} m       "
              f"{med:>12.2f} ft{statistics.mean(diffs):>+11.2f} ft"
              f"{within:>9} / {len(diffs)}")
        if best is None or med < best[1]:
            best = (w, med)

    w, med = best
    print()
    print(f"best fit: {'point sample' if w == 0 else f'+/- {w} m window (~{2 * w} m cells)'}"
          f", median residual {med:.2f} ft")
    if med <= 1.0:
        print("  -> smoothing explains the disagreement: the old elevations came from a")
        print("     coarser DEM, and the line/point coordinates are not implicated.")
    elif med < 3.0:
        print("  -> smoothing explains most of it; a coarser source is the likely origin,")
        print("     with some residual left for coordinate and surface noise.")
    else:
        print("  -> smoothing does NOT explain it; the residual floor stays high, so a")
        print("     horizontal offset remains the better explanation.")

    # Does the residual still track slope at the best window?  If a coarse
    # source is the whole story, the slope dependence should collapse with it.
    print()
    print(f"residual at the best window, by local terrain slope:")
    from .diagnose import measure
    slope_of = {n["id"]: s for s, _d, n in measure(nodes)}
    bands = [(0.0, .08), (.08, .15), (.15, .25), (.25, .40), (.40, 99.0)]
    for lo, up in bands:
        sel = [abs(means[i][w] - stored[i]) for i in means
               if lo <= slope_of.get(i, -1) < up]
        if sel:
            label = f"{100 * lo:.0f}% - {100 * min(up, 1.5):.0f}%"
            print(f"  {label:<14}{len(sel):>6} nodes   median {statistics.median(sel):5.2f} ft")
    return 0


if __name__ == "__main__":
    sys.exit(main())
