"""Sample 3DEP tiles at projected points.

Two resampling methods, because which one a pipeline used is itself a fact worth
recovering: `nearest` returns the raw cell value (what a plain point query or a
default "Extract Values to Points" gives), `bilinear` weights the four cells
around the point (what most interpolate-to-point tools give).  Comparing both
against stored values narrows down how the original elevations were produced.

Reads are windowed -- a 1 m tile is 10012 x 10012 Float32, 400 MB if read whole,
so a point query pulls only the 2x2 cells it needs.  That also makes /vsicurl/
tiles usable directly, since only the touched blocks come over the wire.

Heights come out of the tiles in NAVD88 metres and are returned in feet, the
unit the edge list and the app both use.
"""
from __future__ import annotations

import math

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.windows import Window

from .tiles import NODATA, TILE_CRS

M_TO_FT = 1.0 / 0.3048

_to_utm = Transformer.from_crs("EPSG:4326", TILE_CRS, always_xy=True)

_bounds_cache: dict[str, tuple[float, float, float, float]] = {}


def lonlat_to_utm(lon: float, lat: float) -> tuple[float, float]:
    return _to_utm.transform(lon, lat)


def tile_bounds(path: str):
    if path not in _bounds_cache:
        with rasterio.open(path) as ds:
            _bounds_cache[path] = tuple(ds.bounds)
    return _bounds_cache[path]


def _valid(v) -> bool:
    return v is not None and np.isfinite(v) and v != NODATA and v > -1000


def sample_tile(path: str, pts_xy, method: str = "bilinear"):
    """Sample one tile at [(easting, northing), ...]; None where off-tile or NoData.

    Points outside the raster, or landing on NoData -- and for bilinear, points
    whose 2x2 neighbourhood contains NoData -- come back as None rather than as
    a sentinel, so a caller can never mistake -999999 for a height.
    """
    out: list[float | None] = [None] * len(pts_xy)
    with rasterio.open(path) as ds:
        left, bottom, right, top = ds.bounds
        inv = ~ds.transform

        def cell(r: int, c: int):
            if not (0 <= r < ds.height and 0 <= c < ds.width):
                return None
            v = float(ds.read(1, window=Window(c, r, 1, 1))[0, 0])
            return v if _valid(v) else None

        for i, (x, y) in enumerate(pts_xy):
            if not (left <= x <= right and bottom <= y <= top):
                continue
            col_f, row_f = inv * (x, y)
            # Cell centres sit at (col + 0.5, row + 0.5) in this index space.
            col_c, row_c = col_f - 0.5, row_f - 0.5

            if method == "nearest":
                v = cell(int(round(row_c)), int(round(col_c)))
                if v is not None:
                    out[i] = v * M_TO_FT
                continue

            r0, c0 = math.floor(row_c), math.floor(col_c)
            if not (0 <= r0 < ds.height - 1 and 0 <= c0 < ds.width - 1):
                # Against the very edge, fall back to the nearest cell.
                v = cell(min(max(int(round(row_c)), 0), ds.height - 1),
                         min(max(int(round(col_c)), 0), ds.width - 1))
                if v is not None:
                    out[i] = v * M_TO_FT
                continue

            q = ds.read(1, window=Window(c0, r0, 2, 2)).astype(float)
            if q.shape != (2, 2) or not all(_valid(v) for v in q.ravel()):
                continue
            fr, fc = row_c - r0, col_c - c0
            v = (q[0, 0] * (1 - fr) * (1 - fc) + q[0, 1] * (1 - fr) * fc +
                 q[1, 0] * fr * (1 - fc) + q[1, 1] * fr * fc)
            out[i] = float(v) * M_TO_FT
    return out
