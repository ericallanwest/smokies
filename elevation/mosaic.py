"""A priority mosaic over the 3DEP tiles, without building a mosaic.

Where acquisitions overlap -- 35 of the 87 tiles the park needs -- the tile with
the highest priority wins.  Newest first, which step 1 showed is a near-formality
here: the projects agree to a median of 0.28 ft where they meet, so the ordering
decides almost nothing.  It is written down anyway, because a rule you can read
is worth more than a rule you have to reconstruct.

Sampling goes straight to the winning tile rather than through a VRT, so there
is nothing to rebuild when the priority list changes and no resampled
intermediate to drift from the source.
"""
from __future__ import annotations

import os

import rasterio
from rasterio.windows import Window

from .sample import M_TO_FT
from .tiles import build_index

# Newest acquisition wins.  Anything unlisted sorts last, so a newly downloaded
# project is used only where nothing else covers the ground until it is ranked.
PRIORITY = [
    "NC_Phase5_2018_A18",
    "TN_Eastern_2_16_B16_Del1_2016",
    "TN_Eastern_2_16_B16_Del2_2016",
    "TN_Eastern_2_16_B16_Del3_2016",
    "TN_BlountCo_2015",
]


def _rank(project: str) -> int:
    return PRIORITY.index(project) if project in PRIORITY else len(PRIORITY)


class Mosaic:
    """Point sampler over the tile set, honouring PRIORITY on overlaps."""

    def __init__(self, local_only: bool = True):
        tiles = [t for t in build_index().values() if t.local or not local_only]
        # Best-first, so the first tile that covers a point is the one to use.
        self.tiles = sorted(tiles, key=lambda t: (_rank(t.project), t.project))
        self._ds: dict[str, rasterio.DatasetReader] = {}
        self._bounds: dict[str, tuple] = {}
        for t in self.tiles:
            with rasterio.open(t.path) as ds:
                self._bounds[t.path] = tuple(ds.bounds)

    def _open(self, path: str):
        if path not in self._ds:
            self._ds[path] = rasterio.open(path)
        return self._ds[path]

    def close(self):
        for ds in self._ds.values():
            ds.close()
        self._ds.clear()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def sample(self, pts_xy):
        """Bilinear height in feet at each (easting, northing); None if uncovered."""
        out: list[float | None] = [None] * len(pts_xy)
        remaining = list(range(len(pts_xy)))
        for t in self.tiles:
            if not remaining:
                break
            left, bottom, right, top = self._bounds[t.path]
            here = [i for i in remaining
                    if left <= pts_xy[i][0] <= right and bottom <= pts_xy[i][1] <= top]
            if not here:
                continue
            ds = self._open(t.path)
            inv = ~ds.transform
            still = []
            for i in here:
                x, y = pts_xy[i]
                col_f, row_f = inv * (x, y)
                col_c, row_c = col_f - 0.5, row_f - 0.5
                r0, c0 = int(row_c // 1), int(col_c // 1)
                if not (0 <= r0 < ds.height - 1 and 0 <= c0 < ds.width - 1):
                    still.append(i)
                    continue
                q = ds.read(1, window=Window(c0, r0, 2, 2)).astype(float)
                if q.shape != (2, 2) or (q < -1000).any():
                    still.append(i)
                    continue
                fr, fc = row_c - r0, col_c - c0
                v = (q[0, 0] * (1 - fr) * (1 - fc) + q[0, 1] * (1 - fr) * fc +
                     q[1, 0] * fr * (1 - fc) + q[1, 1] * fr * fc)
                out[i] = float(v) * M_TO_FT
            covered = set(here) - set(still)
            remaining = [i for i in remaining if i not in covered]
        return out
