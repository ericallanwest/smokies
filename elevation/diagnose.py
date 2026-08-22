"""Is the stored elevation wrong, or is the node in the wrong place?

Comparing stored elevations against a fresh 3DEP sample gives a difference, but
not a cause.  Two very different faults produce a difference:

  * a vertical fault -- wrong DEM, wrong datum, wrong units -- shifts values in
    one direction, so the signed differences have a non-zero mean.
  * a horizontal fault -- the node is some distance from the feature it names --
    shifts values in whichever direction the ground happens to fall, so the mean
    stays near zero while the scatter grows *with the local terrain slope*.

The second is diagnosable: bin the differences by the slope of the ground the
node sits on.  If flat-ground nodes agree and steep-ground nodes do not, the
elevations were sampled correctly from a defensible surface at coordinates that
are off the mark, and no amount of DEM curation will fix them.

Dividing |difference| by local slope estimates how far off, in feet.

    python -m elevation.diagnose
"""
from __future__ import annotations

import math
import statistics
import sys

from .forensics import load_nodes
from .sample import sample_tile, tile_bounds
from .tiles import build_index

STENCIL_M = 15.0          # half-width of the slope stencil, in metres
FT_PER_M = 1.0 / 0.3048
BANDS = [(0.0, .08), (.08, .15), (.15, .25), (.25, .40), (.40, 99.0)]


def node_probes(nodes):
    """Five points per node: west, east, south, north, centre."""
    out = {}
    for n in nodes:
        e, y = n["easting"], n["northing"]
        out[n["id"]] = [(e - STENCIL_M, y), (e + STENCIL_M, y),
                        (e, y - STENCIL_M), (e, y + STENCIL_M), (e, y)]
    return out


def measure(nodes):
    """(local slope, signed difference, node) per node the DEM fully covers."""
    probe = node_probes(nodes)
    got: dict[str, list[float]] = {}
    for t in build_index().values():
        if not t.local:
            continue
        left, bottom, right, top = tile_bounds(t.path)
        ids = [n["id"] for n in nodes
               if left <= n["easting"] <= right and bottom <= n["northing"] <= top]
        if not ids:
            continue
        flat = [p for i in ids for p in probe[i]]
        vals = sample_tile(t.path, flat, method="bilinear")
        for k, i in enumerate(ids):
            chunk = vals[k * 5:(k + 1) * 5]
            if all(v is not None for v in chunk):
                got.setdefault(i, chunk)

    rows = []
    for n in nodes:
        v = got.get(n["id"])
        if not v:
            continue
        run = 2 * STENCIL_M * FT_PER_M
        slope = math.hypot((v[1] - v[0]) / run, (v[3] - v[2]) / run)
        rows.append((slope, v[4] - n["stored_ft"], n))
    return rows


def main() -> int:
    nodes = load_nodes()
    rows = measure(nodes)
    if not rows:
        print("no nodes could be sampled -- are the tiles in 3DEP_UTM_17/?")
        return 1

    signed = [d for _s, d, _n in rows]
    print(f"nodes measured : {len(rows)} of {len(nodes)}")
    print()
    print("VERTICAL FAULT?  (a wrong DEM, datum or unit biases the sign)")
    print(f"  signed difference, 3DEP minus stored: mean {statistics.mean(signed):+.2f} ft, "
          f"median {statistics.median(signed):+.2f} ft")
    print(f"  scatter                             : stdev {statistics.pstdev(signed):.2f} ft, "
          f"range {min(signed):+.1f} to {max(signed):+.1f} ft")
    hi = sum(1 for d in signed if d > 0)
    print(f"  3DEP higher on {hi} of {len(signed)} nodes ({100 * hi / len(signed):.0f}%) "
          f"-- a bias would push this far from 50%")

    print()
    print("HORIZONTAL FAULT?  (misplaced nodes cost more on steeper ground)")
    print(f"  {'local slope':<18}{'nodes':>7}{'median |difference|':>22}")
    for lo, up in BANDS:
        sel = [abs(d) for s, d, _n in rows if lo <= s < up]
        if sel:
            label = f"{100 * lo:.0f}% - {100 * min(up, 1.5):.0f}%"
            print(f"  {label:<18}{len(sel):>7}{statistics.median(sel):>19.1f} ft")

    xs = [s for s, _d, _n in rows]
    ys = [abs(d) for _s, d, _n in rows]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
    print()
    print(f"  correlation of |difference| with local slope : r = {num / den:.3f}")

    imp = sorted(abs(d) / s for s, d, _n in rows if s > 0.05)
    if imp:
        print(f"  implied horizontal offset |dz| / slope       : "
              f"median {statistics.median(imp):.0f} ft "
              f"({statistics.median(imp) * 0.3048:.0f} m), "
              f"p90 {imp[int(.9 * len(imp))]:.0f} ft")

    print()
    print("worst nodes (steep ground is where a misplaced node hurts most):")
    for s, d, n in sorted(rows, key=lambda r: -abs(r[1]))[:10]:
        print(f"  {d:+7.1f} ft  slope {100 * s:5.1f}%  {n['id']:<7} {n['name'][:34]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
