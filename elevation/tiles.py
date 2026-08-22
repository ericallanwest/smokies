"""Index the USGS 3DEP 1 m DEM tiles that cover the park.

One tile is one (project, tile-index) pair.  Tile indices repeat across projects
-- 35 of the 87 tiles the park needs are delivered by two or three separate
acquisitions -- so a tile is only identified by its project *and* its index, and
the whole point of the index is to keep those candidates apart rather than
collapsing them the way a mosaic would.

Sources, in the order this module prefers them:
  1. 3DEP_UTM_17/           tiles already downloaded next to the repo
  2. the S3 URLs in smokies/gsmnp_1m_elevation_files.csv, read through /vsicurl/

All tiles are NAD83 / UTM 17N (EPSG:26917), 1 m, Float32, NoData -999999.
"""
from __future__ import annotations

import csv
import math
import os
import re
from dataclasses import dataclass

TILE_CRS = "EPSG:26917"
NODATA = -999999.0

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TILE_DIR = os.path.join(REPO, "3DEP_UTM_17")
MANIFEST_CSV = os.path.join(REPO, "smokies", "gsmnp_1m_elevation_files.csv")

# USGS has shipped 1 m tiles under two naming conventions; both encode the same
# thing: UTM zone, tile easting/northing in units of 10 km, and project name.
#   USGS_1M_17_x22y391_NC_Phase5_2018_A18.tif
#   USGS_one_meter_x22y392_TN_Eastern_2_16_B16_Del1_2016.tif
_NAME_RE = re.compile(
    r"^USGS_(?:1M_(?P<zone>\d+)_|one_meter_)x(?P<x>\d+)y(?P<y>\d+)_(?P<project>.+)\.tif$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Tile:
    project: str
    x: int              # easting / 10 000 m
    y: int              # northing / 10 000 m
    path: str           # local path or /vsicurl/ URL
    local: bool

    @property
    def index(self) -> tuple[int, int]:
        return (self.x, self.y)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    def __str__(self) -> str:
        return f"x{self.x}y{self.y} [{self.project}]"


def _parse(name: str):
    m = _NAME_RE.match(name)
    if not m:
        return None
    return m.group("project"), int(m.group("x")), int(m.group("y"))


def local_tiles() -> list[Tile]:
    """Tiles present in 3DEP_UTM_17/."""
    out = []
    if not os.path.isdir(LOCAL_TILE_DIR):
        return out
    for fn in sorted(os.listdir(LOCAL_TILE_DIR)):
        if not fn.lower().endswith(".tif"):
            continue
        parsed = _parse(fn)
        if parsed is None:
            continue
        project, x, y = parsed
        out.append(Tile(project, x, y, os.path.join(LOCAL_TILE_DIR, fn), True))
    return out


def manifest_tiles() -> list[Tile]:
    """Tiles named by the hand-built manifest, addressed through /vsicurl/."""
    out = []
    if not os.path.exists(MANIFEST_CSV):
        return out
    with open(MANIFEST_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            url = row["url"].strip()
            parsed = _parse(os.path.basename(url))
            if parsed is None:
                continue
            project, x, y = parsed
            out.append(Tile(project, x, y, "/vsicurl/" + url, False))
    return out


def build_index(prefer_local: bool = True) -> dict[tuple[str, int, int], Tile]:
    """All known tiles keyed by (project, x, y), local copies winning."""
    index: dict[tuple[str, int, int], Tile] = {}
    for t in manifest_tiles():
        index[(t.project, t.x, t.y)] = t
    for t in local_tiles():
        key = (t.project, t.x, t.y)
        if prefer_local or key not in index:
            index[key] = t
    return index


def by_tile_index(index: dict) -> dict[tuple[int, int], list[Tile]]:
    """Group by tile index, so contested tiles list every competing project."""
    out: dict[tuple[int, int], list[Tile]] = {}
    for t in index.values():
        out.setdefault(t.index, []).append(t)
    for tiles in out.values():
        tiles.sort(key=lambda t: t.project)
    return out


def tile_index_for(easting: float, northing: float) -> tuple[int, int]:
    """Which 10 km tile a UTM 17N coordinate nominally falls in.

    The naming is not symmetric, which is easy to get wrong: x is the tile's
    WEST edge but y is its NORTH edge, both in units of 10 km.  Tile
    x22y391 has its origin at (219994, 3910006) and runs down and right from
    there, so easting rounds down and northing rounds *up*.

    Tiles ship 10 012 px wide -- a 6 m collar on each side -- so neighbours
    overlap slightly and a point near an edge exists in more than one tile.
    Membership is therefore decided by real raster bounds, not by this
    function; it is here for grouping and reporting only.
    """
    return (int(easting // 10000), int(math.ceil(northing / 10000)))


if __name__ == "__main__":
    idx = build_index()
    groups = by_tile_index(idx)
    projects: dict[str, int] = {}
    for t in idx.values():
        projects[t.project] = projects.get(t.project, 0) + 1
    n_local = sum(1 for t in idx.values() if t.local)

    print(f"tiles indexed        : {len(idx)}  ({n_local} local, "
          f"{len(idx) - n_local} via /vsicurl/)")
    print(f"distinct tile indices: {len(groups)}")
    print()
    print("by project:")
    for p, n in sorted(projects.items(), key=lambda kv: -kv[1]):
        print(f"  {n:3d}  {p}")

    contested = {k: v for k, v in groups.items() if len(v) > 1}
    print()
    print(f"tiles delivered by more than one project: {len(contested)} "
          f"of {len(groups)}")
    for (x, y), tiles in sorted(contested.items()):
        missing = [t for t in tiles if not t.local]
        flag = "" if not missing else f"   ({len(missing)} not downloaded)"
        print(f"  x{x}y{y}: " + ", ".join(t.project for t in tiles) + flag)
