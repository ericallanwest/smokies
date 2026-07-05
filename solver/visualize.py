#!/usr/bin/env python3
"""
visualize.py — Generate a standalone interactive HTML map for the GSMNP itinerary.

Usage:
    python visualize.py <itinerary.json>
    python visualize.py smokies_itinerary_12_10_20260509b.json --out smokies_viz.html
"""

import argparse
import json
import urllib.request
from pathlib import Path

DEFAULT_LINES       = "lines_20250211.geojson"
DEFAULT_POINTS      = "points_20250211.geojson"
DEFAULT_OUT         = "smokies_viz.html"
MAPWARPER_MAP_ID    = "88180"
MAPWARPER_FALLBACK  = [[35.24, -84.10], [35.89, -82.97]]

ICON_MAP = {
    "BC": "campsite",
    "SH": "shelter",
    "CG": "trailer-site",
    "TH": "trailhead",
    "TI": "sign",
    "RI": "sign",
}


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def to_latlng(coords):
    """GeoJSON [lon,lat,?z] → [lat,lon] rounded to 5 dp (≈1 m precision)."""
    return [[round(c[1], 5), round(c[0], 5)] for c in coords]


def fmt_hm(seconds):
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h {m:02d}m"


def node_type_label(node_id):
    if node_id.startswith("BC"):  return "Backcountry Campsite"
    if node_id.startswith("SH"):  return "Shelter"
    if node_id.startswith("CG"):  return "Campground"
    if node_id.startswith("TH"):  return "Trailhead"
    if node_id.startswith("TI"):  return "Trail Intersection"
    if node_id.startswith("RI"):  return "Road Intersection"
    return "Node"


# ---------------------------------------------------------------------------
# Directed geometry lookup from GeoJSON
# ---------------------------------------------------------------------------

def build_directed_geom(lines_gj):
    """
    Returns (geom, named_geom).
      geom:       {(Start, End): coords} — first-wins, used for deadhead/fallback
      named_geom: {(Start, End, Name): coords} — all features, used to distinguish
                  parallel trails that share the same endpoint pair
    """
    geom  = {}
    named = {}
    for feat in lines_gj["features"]:
        p    = feat["properties"]
        s, e = p.get("Start", ""), p.get("End", "")
        if not s or not e:
            continue
        name   = p.get("Name", "")
        latlng = to_latlng(feat["geometry"]["coordinates"])
        if (s, e) not in geom:
            geom[(s, e)] = latlng
        if (e, s) not in geom:
            geom[(e, s)] = list(reversed(latlng))
        named[(s, e, name)] = latlng
        named[(e, s, name)] = list(reversed(latlng))
    return geom, named


# ---------------------------------------------------------------------------
# Node coordinates from points GeoJSON
# ---------------------------------------------------------------------------

def build_node_coords(points_gj):
    coords = {}
    for feat in points_gj["features"]:
        nid = feat["properties"].get("id", "")
        if not nid:
            continue
        lon, lat = feat["geometry"]["coordinates"][:2]
        coords[nid] = [round(lat, 5), round(lon, 5)]
    return coords


# ---------------------------------------------------------------------------
# All nodes for the NPS-icon toggle layer
# ---------------------------------------------------------------------------

def build_all_nodes(points_gj):
    nodes = []
    for feat in points_gj["features"]:
        p   = feat["properties"]
        nid = p.get("id", "")
        if not nid:
            continue
        lon, lat = feat["geometry"]["coordinates"][:2]
        nodes.append({
            "id":     nid,
            "name":   p.get("name", nid),
            "type":   node_type_label(nid),
            "icon":   ICON_MAP.get(nid[:2], "sign"),
            "coords": [round(lat, 5), round(lon, 5)],
        })
    return nodes


# ---------------------------------------------------------------------------
# Unified GEOM dict + per-day data
# ---------------------------------------------------------------------------

def undirected_key(u, v):
    return "|".join(sorted([u, v]))


def build_geom_and_days(itinerary, directed_geom, named_geom, node_coords):
    """
    Returns:
        geom_dict: {key: [[lat,lon],...]} — geometry keyed by str(edge_id) for
                   required arcs (so parallel trails get distinct polylines) and
                   by undirected_key for deadhead arcs
        days_data: list of per-day dicts, each arc tagged with geom_fwd bool

    geom_fwd=True  → GEOM[key] coords run in the same direction as the arc (u→v)
    geom_fwd=False → GEOM[key] coords need to be reversed to match arc direction
    """
    geom_dict      = {}
    geom_dir_cache = {}

    def resolve_coords(u, v, trail_name=None, edge_id=None):
        # Required arcs get a per-edge key so parallel trails each have their
        # own entry in GEOM; deadhead arcs share the first-wins ukey.
        gkey = str(edge_id) if edge_id is not None else undirected_key(u, v)
        if gkey not in geom_dict:
            # Try named lookup first — handles parallel edges with same endpoints
            if trail_name and (u, v, trail_name) in named_geom:
                geom_dict[gkey]      = named_geom[(u, v, trail_name)]
                geom_dir_cache[gkey] = u
            elif trail_name and (v, u, trail_name) in named_geom:
                geom_dict[gkey]      = named_geom[(v, u, trail_name)]
                geom_dir_cache[gkey] = v
            elif (u, v) in directed_geom:
                geom_dict[gkey]      = directed_geom[(u, v)]
                geom_dir_cache[gkey] = u
            elif (v, u) in directed_geom:
                geom_dict[gkey]      = directed_geom[(v, u)]
                geom_dir_cache[gkey] = v
            else:
                c1, c2 = node_coords.get(u), node_coords.get(v)
                if c1 and c2:
                    geom_dict[gkey] = [c1, c2]
                geom_dir_cache[gkey] = u
        geom_fwd = (geom_dir_cache.get(gkey, u) == u)
        return gkey, geom_fwd

    days_data        = []
    cum_req_miles    = 0.0
    covered_edge_ids = set()   # edge_id set, for deduplication

    for day_info in itinerary["days"]:
        steps = []
        day_req_miles_walked = 0.0
        day_new_req_miles    = 0.0
        day_total_s = day_req_s = day_dh_s = 0
        day_miles   = 0.0
        day_gain    = 0

        for arc in day_info["arcs"]:
            u, v  = arc["from"], arc["to"]
            is_dh = arc["is_deadhead"]
            eid   = arc.get("edge_id")
            tname = arc.get("trail") if not is_dh else None
            gkey, geom_fwd = resolve_coords(u, v,
                                            trail_name=tname,
                                            edge_id=(None if is_dh else eid))
            day_total_s += arc["seconds"]
            day_miles   += arc["miles"]
            day_gain    += arc["gain"]

            _tname = _trail_label(arc["trail"]) if not is_dh else arc["trail"]
            popup = (
                f"<b>{_tname}</b><br>"
                f"{u} → {v}<br>"
                f"{arc['miles']:.2f} mi &nbsp; {fmt_hm(arc['seconds'])}<br>"
                f"+{arc['gain']:,} ft"
            )
            entry = {
                "key":      gkey,
                "eid":      eid,
                "geom_fwd": geom_fwd,
                "trail":    _tname,
                "from":     u,
                "to":       v,
                "miles":    arc["miles"],
                "seconds":  arc["seconds"],
                "gain":     arc["gain"],
                "popup":    popup,
                "is_dh":    is_dh,
            }
            steps.append(entry)

            if arc["is_deadhead"]:
                day_dh_s += arc["seconds"]
            else:
                day_req_s            += arc["seconds"]
                day_req_miles_walked += arc["miles"]
                eid = arc.get("edge_id")
                if eid is not None and eid not in covered_edge_ids:
                    covered_edge_ids.add(eid)
                    day_new_req_miles += arc["miles"]

        cum_req_miles += day_new_req_miles

        days_data.append({
            "day":           day_info["day"],
            "start_node":    day_info["start_node"],
            "end_node":      day_info["end_node"],
            "total_s":       day_total_s,
            "req_s":         day_req_s,
            "dh_s":          day_dh_s,
            "miles":         round(day_miles, 2),
            "gain":          int(day_gain),
            "req_miles":     round(day_req_miles_walked, 2),
            "cum_req_miles": round(cum_req_miles, 2),
            "start_coords":  node_coords.get(day_info["start_node"]),
            "end_coords":    node_coords.get(day_info["end_node"]),
            "steps":         steps,
        })

    return geom_dict, days_data


def _trail_label(name):
    """Append ' Trail' to trail name unless it already ends with 'trail'."""
    if not name or name.lower().endswith("trail"):
        return name
    return name + " Trail"


def build_bg_layer(itinerary):
    """Unique required edge keys (geometry looked up from GEOM in JS)."""
    seen = set()
    bg   = []
    for day_info in itinerary["days"]:
        for arc in day_info["arcs"]:
            if arc["is_deadhead"]:
                continue
            eid = arc.get("edge_id")
            if eid in seen:
                continue
            seen.add(eid)
            bg.append({
                "key":     str(eid),   # matches str(edge_id) key used in geom_dict
                "eid":     eid,
                "trail":   _trail_label(arc["trail"]),
                "from":    arc["from"],
                "to":      arc["to"],
                "miles":   arc["miles"],
                "gain":    arc["gain"],
                "seconds": arc["seconds"],
            })
    return bg


# ---------------------------------------------------------------------------
# HTML rendering
def build_optional_layer(lines_gj, required_keys, geom_dict):
    """Segments not in required_keys. Adds their geometry to geom_dict in-place."""
    seen = set()
    opt  = []
    for feat in lines_gj["features"]:
        p    = feat["properties"]
        s, e = p.get("Start", ""), p.get("End", "")
        if not s or not e:
            continue
        ukey = undirected_key(s, e)
        if ukey in required_keys or ukey in seen:
            continue
        seen.add(ukey)
        if ukey not in geom_dict:
            geom_dict[ukey] = to_latlng(feat["geometry"]["coordinates"])
        try:
            miles = float(p.get("Miles") or 0)
            gain  = int(float(p.get("Gain") or 0))
        except (ValueError, TypeError):
            miles, gain = 0.0, 0
        opt.append({
            "key":   ukey,
            "trail": p.get("Name", ""),
            "from":  s,
            "to":    e,
            "miles": round(miles, 2),
            "gain":  gain,
        })
    return opt


# ---------------------------------------------------------------------------

def fetch_mapwarper_bounds(map_id, timeout=10):
    """Return [[south,west],[north,east]] from MapWarper API, or fallback on failure."""
    url = f"https://mapwarper.net/maps/{map_id}.json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read())
        w, s, e, n = [float(x) for x in data["items"]["bbox"].split(",")]
        bounds = [[round(s, 5), round(w, 5)], [round(n, 5), round(e, 5)]]
        print(f"  MapWarper bbox: {bounds}")
        return bounds
    except Exception as exc:
        print(f"  Warning: MapWarper bbox fetch failed ({exc}); using fallback.")
        return MAPWARPER_FALLBACK


def render_html(meta, geom_dict, days_data, bg_layer, opt_layer, all_nodes, home_bounds):
    geom_json      = json.dumps(geom_dict,  separators=(",", ":"))
    days_json      = json.dumps(days_data,  separators=(",", ":"))
    bg_json        = json.dumps(bg_layer,   separators=(",", ":"))
    opt_json       = json.dumps(opt_layer,  separators=(",", ":"))
    nodes_json     = json.dumps(all_nodes,  separators=(",", ":"))
    meta_json      = json.dumps(meta,       separators=(",", ":"))
    bounds_json    = json.dumps(home_bounds)
    n_days         = meta["n_days"]
    circuit        = meta["circuit"]
    total_req_mi   = meta["total_required_miles"]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>GSMNP {n_days}-Day Itinerary — {circuit} Circuit</title>
<link rel="icon" type="image/svg+xml"
      href="https://raw.githubusercontent.com/nationalparkservice/symbol-library/gh-pages/src/standalone/self-guiding-trail-black-22.svg">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
*{{ box-sizing:border-box; margin:0; padding:0; }}
body{{ font-family:'Segoe UI',sans-serif; font-size:13px;
       background:#f4f5f7; color:#1a1a2e; overflow:hidden; }}
#map{{ position:absolute; top:0; left:0; right:340px; bottom:185px; }}
#sidebar{{ position:absolute; top:0; right:0; width:340px; bottom:185px;
           background:#ffffff; padding:14px 16px; overflow-y:auto;
           border-left:1px solid #d0d3da; }}
#controls{{ position:absolute; bottom:0; left:0; right:0; height:185px;
            background:#ffffff; border-top:1px solid #d0d3da;
            display:flex; flex-direction:column; align-items:center;
            justify-content:center; gap:6px; padding:8px 20px; }}
.day-header{{ font-size:17px; font-weight:700; color:#c0392b; margin-bottom:3px; }}
.day-route{{ font-size:12px; color:#666677; margin-bottom:12px; }}
.sec-title{{ font-size:10px; text-transform:uppercase; letter-spacing:.06em;
             color:#c0392b; margin:12px 0 6px; }}
.stat-grid{{ display:grid; grid-template-columns:1fr 1fr; gap:3px 10px; margin-bottom:4px; }}
.stat-lbl{{ color:#666677; font-size:11px; }}
.stat-val{{ color:#1a1a2e; font-size:13px; font-weight:600; }}
.prog-bg{{ background:#e8eaed; border-radius:4px; height:13px; margin-bottom:3px; }}
.prog-fill{{ height:13px; border-radius:4px;
             background:linear-gradient(90deg,#27ae60,#1a7a42);
             transition:width .35s; }}
.prog-lbl{{ font-size:11px; color:#666677; display:flex; justify-content:space-between; }}
.badge{{ background:#d5f5e3; color:#1a7a42; padding:4px 10px; border-radius:12px;
         font-size:12px; text-align:center; margin-top:8px; display:none; }}
.legend{{ line-height:2.0; font-size:12px; }}
.leg-line{{ display:inline-block; width:18px; height:3px; border-radius:1px;
            vertical-align:middle; margin-right:5px; }}
.slider-row{{ display:flex; align-items:center; gap:7px; width:100%; max-width:960px; }}
#daySlider{{ flex:1; accent-color:#c0392b; cursor:pointer; }}
.btn{{ background:#e8eaed; border:1px solid #c0392b; color:#1a1a2e;
       padding:4px 11px; border-radius:4px; cursor:pointer; font-size:12px;
       white-space:nowrap; }}
.btn:hover{{ background:#c0392b; color:#fff; }}
.day-lbl{{ color:#c0392b; font-weight:700; font-size:13px;
           min-width:90px; text-align:center; }}
.toggle-row{{ display:flex; gap:14px; align-items:center; flex-wrap:wrap; }}
.toggle-row label{{ display:flex; align-items:center; gap:4px; cursor:pointer;
                    font-size:11px; color:#666677; }}
.toggle-row input[type=checkbox]{{ accent-color:#c0392b; cursor:pointer; }}
.opacity-row{{ display:flex; align-items:center; gap:8px; font-size:11px; color:#666677; }}
#mapwarpOpacity{{ width:110px; accent-color:#c0392b; cursor:pointer; }}
.nps-icon {{ filter: drop-shadow(0 0 3px #fff) drop-shadow(0 0 6px #fff); }}
.grayscale-layer {{ filter: grayscale(100%); }}
.itin-step:hover {{ background:#f4f5f7; }}
body.zoom-small-icons .nps-icon {{ transform: scale(0.6); }}
#map{{ transition: right 0.25s ease; }}
#sidebar{{ transition: width 0.25s ease, padding 0.25s ease; }}
body.sidebar-hidden #map{{ right: 0; }}
body.sidebar-hidden #sidebar{{ width: 0; padding: 0; overflow: hidden; }}
#sidebar-toggle{{
  position: absolute; top: 50%; right: 340px; transform: translateY(-50%);
  width: 16px; height: 52px; background: #c0392b; color: #fff;
  border: none; border-radius: 4px 0 0 4px; cursor: pointer; font-size: 11px;
  display: flex; align-items: center; justify-content: center;
  z-index: 1000; transition: right 0.25s ease;
}}
#sidebar-toggle:hover{{ background: #a93226; }}
body.sidebar-hidden #sidebar-toggle{{ right: 0; }}
</style>
</head>
<body>

<div id="map"></div>

<button id="sidebar-toggle" title="Hide/show sidebar">▶</button>
<div id="sidebar">
  <div class="day-header" id="sbDay">Day 1 of {n_days}</div>
  <div class="day-route"  id="sbRoute">— → —</div>
  <div class="sec-title">Today</div>
  <div class="stat-grid">
    <span class="stat-lbl">Total time</span>  <span class="stat-val" id="sbTime">—</span>
    <span class="stat-lbl">Required</span>    <span class="stat-val" id="sbReq">—</span>
    <span class="stat-lbl">Deadhead</span>    <span class="stat-val" id="sbDH">—</span>
    <span class="stat-lbl">Miles</span>       <span class="stat-val" id="sbMiles">—</span>
    <span class="stat-lbl">Gain</span>        <span class="stat-val" id="sbGain">—</span>
    <span class="stat-lbl">Req miles</span>   <span class="stat-val" id="sbReqMi">—</span>
  </div>
  <div class="sec-title">Map Completion</div>
  <div class="prog-bg"><div class="prog-fill" id="progFill" style="width:0%"></div></div>
  <div class="prog-lbl">
    <span id="progPct">0%</span>
    <span id="progMi">0 / {total_req_mi:.1f} req mi</span>
  </div>
  <div class="badge" id="completeBadge">✓ All required trails covered!</div>
  <div class="sec-title">Legend</div>
  <div class="legend">
    <div><span class="leg-line" style="background:#999"></span>Required — not yet covered</div>
    <div><span class="leg-line" style="background:#333333"></span>Required — covered (cumulative)</div>
    <div><span class="leg-line" style="background:#f7882f"></span>Today — required</div>
    <div><span class="leg-line" style="background:#c0392b;border-top:2px dashed #c0392b"></span>Today — deadhead</div>
  </div>
  <div class="sec-title">Today's Route</div>
  <div id="itinerary" style="font-size:11px;line-height:1.7;"></div>
</div>

<div id="controls">
  <div class="slider-row">
    <button class="btn" id="btnHome">⌂ Home</button>
    <button class="btn" id="btnPrev">← Day</button>
    <input  type="range" id="daySlider" min="1" max="{n_days}" value="1">
    <span   class="day-lbl" id="dayLbl">Day 1 / {n_days}</span>
    <button class="btn" id="btnNext">Day →</button>
    <button class="btn" id="btnPlay">▶ Play</button>
    <button class="btn" id="btnZoom">⊕ Zoom to Day</button>
  </div>
  <div class="slider-row" id="stepRow">
    <button class="btn" id="btnStepPrev">← Step</button>
    <input type="range" id="stepSlider" min="1" max="1" value="1"
           style="flex:1;accent-color:#f7882f;cursor:pointer">
    <span class="day-lbl" id="stepLbl" style="min-width:120px;font-size:12px">Step 1 / 1</span>
    <button class="btn" id="btnStepNext">Step →</button>
    <button class="btn" id="btnStepPlay">▶ Play</button>
    <button class="btn" id="btnZoomStep">⊕ Zoom to Step</button>
  </div>
  <div class="toggle-row">
    <label><input type="checkbox" id="togDH"              checked> Backtracking</label>
    <label><input type="checkbox" id="togOpt">           Optional</label>
    <span style="margin:0 2px;color:#ccc">|</span>
    <label><input type="checkbox" id="togTrailheads">    Trailheads</label>
    <label><input type="checkbox" id="togCamping">       Camping</label>
    <label><input type="checkbox" id="togIntersections"> Intersections</label>
  </div>
  <div class="opacity-row">
    <span>Trail Map</span>
    <input type="range" id="mapwarpOpacity" min="0" max="100" step="5" value="40">
    <span id="opacityVal">40%</span>
    <span style="margin:0 6px;color:#ccc">|</span>
    <span>Hillshade</span>
    <input type="range" id="hillshadeOpacity" min="0" max="100" step="5" value="15" style="width:110px;accent-color:#c0392b;cursor:pointer">
    <span id="hillshadeOpacityVal">15%</span>
    <span style="margin:0 6px;color:#ccc">|</span>
    <span>Basemap</span>
    <input type="range" id="basemapOpacity" min="0" max="100" step="5" value="15" style="width:110px;accent-color:#c0392b;cursor:pointer">
    <span id="basemapOpacityVal">15%</span>
    <select id="basemapSel" style="font-size:11px;border:1px solid #d0d3da;border-radius:3px;padding:2px 4px;cursor:pointer;margin-left:4px">
      <option>OSM Grayscale</option>
      <option>OSM Color</option>
      <option>CartoDB Light</option>
      <option>Google Maps</option>
      <option>Bing Aerial</option>
      <option>ESRI World Topo</option>
    </select>
  </div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.jsdelivr.net/npm/leaflet-polylinedecorator@1.6.0/dist/leaflet.polylineDecorator.js"></script>
<script>
// ── Embedded data ──────────────────────────────────────────────────────────
const META      = {meta_json};
const GEOM      = {geom_json};
const BG        = {bg_json};
const OPT       = {opt_json};
const ALL_NODES = {nodes_json};
const DAYS      = {days_json};

// ── Precompute cumulative coverage sets ────────────────────────────────────
const cumCov = [];
{{
  const seen = new Set();
  for (const d of DAYS) {{
    for (const a of d.steps) {{ if (!a.is_dh) seen.add(a.eid); }}
    cumCov.push(new Set(seen));
  }}
}}

// ── Map ────────────────────────────────────────────────────────────────────
const HOME_BOUNDS = {bounds_json};
const HOME_ZOOM   = 10;
const map = L.map('map');
map.fitBounds(HOME_BOUNDS);

const _BingLayer = L.TileLayer.extend({{
  getTileUrl: function(coords) {{
    let q = '';
    for (let i = coords.z; i > 0; i--) {{
      let d = 0, m = 1 << (i - 1);
      if (coords.x & m) d++;
      if (coords.y & m) d += 2;
      q += d;
    }}
    return 'https://ecn.t3.tiles.virtualearth.net/tiles/a' + q + '.jpeg?g=1';
  }}
}});

const BASEMAPS = {{
  'OSM Grayscale': L.tileLayer(
    'https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
    {{attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', maxZoom:19, zIndex:1, className:'grayscale-layer'}}),
  'OSM Color': L.tileLayer(
    'https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
    {{attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', maxZoom:19, zIndex:1}}),
  'CartoDB Light': L.tileLayer(
    'https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png',
    {{attribution:'&copy; OpenStreetMap contributors &copy; CARTO', maxZoom:19, zIndex:1}}),
  'Google Maps': L.tileLayer(
    'https://mt1.google.com/vt/lyrs=m&x={{x}}&y={{y}}&z={{z}}',
    {{attribution:'&copy; Google', maxZoom:20, zIndex:1}}),
  'Bing Aerial': new _BingLayer('', {{attribution:'&copy; Microsoft Bing', maxZoom:19, zIndex:1}}),
  'ESRI World Topo': L.tileLayer(
    'https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{{z}}/{{y}}/{{x}}',
    {{attribution:'Tiles &copy; Esri', maxZoom:19, zIndex:1}}),
}};
let activeBasemap = BASEMAPS['OSM Grayscale'].addTo(map);
activeBasemap.setOpacity(0.15);

const mapwarperLayer = L.tileLayer(
  'https://mapwarper.net/maps/tile/88180/{{z}}/{{x}}/{{y}}.png',
  {{attribution:'Trail map &copy; NPS via <a href="https://mapwarper.net/maps/88180">MapWarper</a>',
    maxZoom:18, opacity:0.4, zIndex:3}}
).addTo(map);

const hillshadeLayer = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/Elevation/World_Hillshade/MapServer/tile/{{z}}/{{y}}/{{x}}',
  {{attribution:'Hillshade &copy; Esri', maxZoom:16, opacity:0.15, zIndex:2}}
).addTo(map);

// ── Layer groups ───────────────────────────────────────────────────────────
const optGroup     = L.layerGroup();           // optional segments, off by default
const bgGroup      = L.layerGroup().addTo(map);
const covGroup     = L.layerGroup().addTo(map);
const reqGroup     = L.layerGroup().addTo(map);
const dhGroup      = L.layerGroup().addTo(map);
const arrowGroup   = L.layerGroup().addTo(map);
const intersectionGroup = L.layerGroup();
const campingGroup      = L.layerGroup();
const trailheadGroup    = L.layerGroup();
let startMarker = null, endMarker = null;

// ── Background: required edges (always visible, light gray) ────────────────
let selectedBgPoly = null;
const BG_NORMAL = {{color:'#999', weight:5, opacity:0.75}};
const BG_SEL    = {{color:'#FFD700', weight:7, opacity:1}};

for (const seg of BG) {{
  const coords = GEOM[seg.key];
  if (!coords || coords.length < 2) continue;
  const popup = `<b>${{seg.trail}}</b><br>${{seg.from}} ↔ ${{seg.to}}<br>${{seg.miles.toFixed(2)}} mi &nbsp; ${{fmtHM(seg.seconds)}}<br>+${{seg.gain.toLocaleString()}} ft`;
  const poly = L.polyline(coords, {{...BG_NORMAL}})
    .bindTooltip(seg.trail, {{sticky:true, opacity:0.85}})
    .bindPopup(popup);
  poly.on('click', (e) => {{
    L.DomEvent.stopPropagation(e);
    if (selectedBgPoly) selectedBgPoly.setStyle(BG_NORMAL);
    selectedBgPoly = poly;
    poly.setStyle(BG_SEL);
  }});
  poly.addTo(bgGroup);
}}
map.on('click', () => {{
  if (selectedBgPoly) {{ selectedBgPoly.setStyle(BG_NORMAL); selectedBgPoly = null; }}
  if (selectedOptPoly) {{ selectedOptPoly.setStyle(OPT_NORMAL); selectedOptPoly = null; }}
}});

// ── Optional (non-required) segments ──────────────────────────────────────
let selectedOptPoly = null;
const OPT_NORMAL = {{color:'#f08080', weight:5, opacity:0.75}};
const OPT_SEL    = {{color:'#FFD700', weight:7, opacity:1}};

for (const seg of OPT) {{
  const coords = GEOM[seg.key];
  if (!coords || coords.length < 2) continue;
  const popup = `<b>${{seg.trail}}</b><br>${{seg.from}} ↔ ${{seg.to}}<br>${{seg.miles.toFixed(2)}} mi<br>+${{seg.gain.toLocaleString()}} ft`;
  const poly = L.polyline(coords, {{...OPT_NORMAL}})
    .bindTooltip(seg.trail, {{sticky:true, opacity:0.85}})
    .bindPopup(popup);
  poly.on('click', (e) => {{
    L.DomEvent.stopPropagation(e);
    if (selectedOptPoly) selectedOptPoly.setStyle(OPT_NORMAL);
    selectedOptPoly = poly;
    poly.setStyle(OPT_SEL);
  }});
  poly.addTo(optGroup);
}}

// ── NPS icon markers for all nodes ────────────────────────────────────────
const NPS_BASE = 'https://raw.githubusercontent.com/nationalparkservice/symbol-library/gh-pages/src/standalone/';
const _iconCache = {{}};
function npsIcon(name) {{
  if (!_iconCache[name])
    _iconCache[name] = L.icon({{
      iconUrl: NPS_BASE + name + '-black-22.svg',
      iconSize: [22, 22], iconAnchor: [11, 11], popupAnchor: [0, -12],
      className: 'nps-icon'
    }});
  return _iconCache[name];
}}

for (const n of ALL_NODES) {{
  if (!n.coords) continue;
  const pfx = n.id.slice(0,2);
  const grp = ['TI','RI'].includes(pfx) ? intersectionGroup
            : ['BC','SH','CG'].includes(pfx) ? campingGroup
            : trailheadGroup;
  L.marker(n.coords, {{icon: npsIcon(n.icon)}})
   .bindPopup(`<b>${{n.name}}</b><br>${{n.type}}<br><small>${{n.id}}</small>`)
   .addTo(grp);
}}

// ── Helpers ────────────────────────────────────────────────────────────────
function fmtHM(s) {{
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
  return h + 'h ' + String(m).padStart(2,'0') + 'm';
}}

// Return coordinates oriented in the arc's direction of travel.
function getCoords(a) {{
  const raw = GEOM[a.key];
  if (!raw) return null;
  return a.geom_fwd ? raw : [...raw].reverse();
}}

// Draw a polyline + direction arrowheads via PolylineDecorator.
function addPolyDecorated(lineGrp, arrowGrp, a, lineOpts, arrowColor) {{
  const coords = getCoords(a);
  if (!coords || coords.length < 2) return;
  const pl = L.polyline(coords, lineOpts);
  if (a.popup) pl.bindPopup(a.popup);
  if (a.trail) pl.bindTooltip(a.trail, {{sticky:true, opacity:0.85}});
  pl.addTo(lineGrp);
  const mkArrow = (color, wt) => L.polylineDecorator(pl, {{
    patterns: [{{
      offset:'10%', repeat:'120px',
      symbol: L.Symbol.arrowHead({{
        pixelSize: 12, polygon: false,
        pathOptions: {{color:color, weight:wt, opacity:0.9, fillOpacity:0}}
      }})
    }}]
  }});
  mkArrow('#fff', 6).addTo(arrowGrp);
  mkArrow(arrowColor, 2).addTo(arrowGrp);
}}

// Plain polyline without arrows (for background/covered layers).
function addPoly(group, coords, opts, popup, tooltip) {{
  if (!coords || coords.length < 2) return;
  const pl = L.polyline(coords, opts);
  if (popup)   pl.bindPopup(popup);
  if (tooltip) pl.bindTooltip(tooltip, {{sticky:true, opacity:0.85}});
  pl.addTo(group);
}}

function triIcon(color, up) {{
  const pts = up ? '9,2 17,16 1,16' : '1,2 17,2 9,16';
  return L.divIcon({{
    html: `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18"><polygon points="${{pts}}" fill="${{color}}" stroke="#fff" stroke-width="2" stroke-linejoin="round"/></svg>`,
    className: '', iconSize: [18,18], iconAnchor: [9,9]
  }});
}}

let currentDay    = 1;
let playTimer     = null;
let stepPlayTimer = null;
const showDH     = () => document.getElementById('togDH').checked;

// ── Node name lookup ───────────────────────────────────────────────────────
const NODE_NAME = Object.fromEntries(ALL_NODES.map(n => [n.id, n.name]));
function nodeName(id) {{ return NODE_NAME[id] || id; }}

// ── Step-through ───────────────────────────────────────────────────────────
let currentStep = 0;

function renderForStep(d, step) {{
  const day = DAYS[d - 1];
  covGroup.clearLayers(); reqGroup.clearLayers();
  dhGroup.clearLayers();  arrowGroup.clearLayers();

  const cov = cumCov[d - 1];
  for (const seg of BG) {{
    if (!cov.has(seg.eid)) continue;
    addPoly(covGroup, GEOM[seg.key], {{color:'#333333', weight:5, opacity:0.85}},
            `<b>${{seg.trail}}</b><br>${{seg.from}} ↔ ${{seg.to}}<br>${{seg.miles.toFixed(2)}} mi &nbsp; ${{fmtHM(seg.seconds)}}<br>+${{seg.gain.toLocaleString()}} ft`,
            seg.trail);
  }}

  const isFull = (step >= day.steps.length);
  const count  = isFull ? day.steps.length : step;
  for (let i = 0; i < count; i++) {{
    const s      = day.steps[i];
    const isLast = (i === count - 1);
    let lineOpts, arrowColor;
    if (isLast) {{
      lineOpts   = {{color:'#FFD700', weight:7, opacity:1}};
      arrowColor = '#FFD700';
    }} else if (s.is_dh) {{
      if (!showDH()) continue;
      lineOpts   = {{color:'#c0392b', weight:5, opacity:0.9, dashArray:'6,5'}};
      arrowColor = '#c0392b';
    }} else {{
      lineOpts   = {{color:'#f7882f', weight:5, opacity:1}};
      arrowColor = '#f7882f';
    }}
    addPolyDecorated(s.is_dh ? dhGroup : reqGroup, arrowGroup, s, lineOpts, arrowColor);
  }}
}}

// ── Itinerary helpers ──────────────────────────────────────────────────────
function buildItinerary(d) {{
  const day = DAYS[d - 1];
  const el  = document.getElementById('itinerary');
  el.innerHTML = day.steps.map((s, i) => {{
    const dot   = s.is_dh ? '#c0392b' : '#f7882f';
    const extra = s.is_dh
      ? ' <span style="color:#c0392b;font-size:10px">(backtrack)</span>' : '';
    return `<div class="itin-step" data-step="${{i+1}}"
      style="padding:3px 4px 3px 6px;border-radius:3px;cursor:pointer;border-left:3px solid transparent">
      <span style="color:${{dot}};font-weight:700">${{i+1}}.</span>
      <b>${{s.trail}}</b>${{extra}}<br>
      <span style="color:#666677;padding-left:12px">
        ${{nodeName(s.from)}} → ${{nodeName(s.to)}}<br>
        ${{s.miles.toFixed(2)}} mi &nbsp; ${{fmtHM(s.seconds)}} &nbsp; +${{s.gain.toLocaleString()}} ft
      </span>
    </div>`;
  }}).join('');
  el.querySelectorAll('.itin-step').forEach(row => {{
    row.addEventListener('click', () => {{
      const s  = +row.dataset.step;
      const sl = document.getElementById('stepSlider');
      sl.value = s;
      sl.dispatchEvent(new Event('input'));
    }});
  }});
}}

function highlightItineraryStep(step) {{
  const n      = DAYS[currentDay - 1].steps.length;
  const isFull = (step >= n);
  document.querySelectorAll('.itin-step').forEach((row, i) => {{
    const active          = !isFull && (i + 1 === step);
    row.style.background  = active ? '#fff9e6' : '';
    row.style.borderLeft  = active ? '3px solid #FFD700' : '3px solid transparent';
  }});
  if (!isFull && step > 0) {{
    const row = document.querySelector(`.itin-step[data-step="${{step}}"]`);
    if (row) row.scrollIntoView({{block:'nearest'}});
  }}
}}

// ── Main day renderer ──────────────────────────────────────────────────────
function updateDay(d) {{
  currentDay = d;
  const day = DAYS[d - 1];
  const cov = cumCov[d - 1];
  const tot = META.total_required_miles;

  covGroup.clearLayers();
  reqGroup.clearLayers();
  dhGroup.clearLayers();
  arrowGroup.clearLayers();
  if (startMarker) {{ startMarker.remove(); startMarker = null; }}
  if (endMarker)   {{ endMarker.remove();   endMarker   = null; }}
  if (selectedBgPoly) {{ selectedBgPoly.setStyle(BG_NORMAL); selectedBgPoly = null; }}

  const lbl = true;

  // Covered — cumulative green (no arrows)
  for (const seg of BG) {{
    if (!cov.has(seg.eid)) continue;
    addPoly(covGroup, GEOM[seg.key], {{color:'#333333', weight:5, opacity:0.85}},
            `<b>${{seg.trail}}</b><br>${{seg.from}} ↔ ${{seg.to}}<br>${{seg.miles.toFixed(2)}} mi &nbsp; ${{fmtHM(seg.seconds)}}<br>+${{seg.gain.toLocaleString()}} ft`,
            seg.trail);
  }}

  // Today required — orange + arrows
  const reqArcs = day.steps.filter(s => !s.is_dh);
  for (const a of reqArcs) {{
    addPolyDecorated(reqGroup, arrowGroup, a,
      {{color:'#f7882f', weight:5, opacity:1}}, '#f7882f');
  }}

  // Today deadhead — red dashed + arrows (when visible)
  if (showDH()) {{
    const dhArcs = day.steps.filter(s => s.is_dh);
    for (const a of dhArcs) {{
      addPolyDecorated(dhGroup, arrowGroup, a,
        {{color:'#c0392b', weight:5, opacity:0.9, dashArray:'6,5'}}, '#c0392b');
    }}
  }}

  // Start marker (blue)
  if (day.start_coords) {{
    startMarker = L.marker(day.start_coords, {{icon:triIcon('#27ae60',true), zIndexOffset:1000}})
      .bindPopup(`<b>Day ${{d}} Start</b><br>${{day.start_node}}`).addTo(map);
  }}

  // End / camp marker (red)
  if (day.end_coords) {{
    endMarker = L.marker(day.end_coords, {{icon:triIcon('#c0392b',false), zIndexOffset:1001}})
      .bindPopup(`<b>Day ${{d}} Camp</b><br>${{day.end_node}}`).addTo(map);
  }}

  // Sidebar stats
  document.getElementById('sbDay').textContent    = `Day ${{d}} of ${{META.n_days}}`;
  document.getElementById('sbRoute').textContent  = `${{nodeName(day.start_node)}} → ${{nodeName(day.end_node)}}`;
  document.getElementById('sbTime').textContent   = fmtHM(day.total_s);
  document.getElementById('sbReq').textContent    = fmtHM(day.req_s);
  document.getElementById('sbDH').textContent     = fmtHM(day.dh_s);
  document.getElementById('sbMiles').textContent  = day.miles.toFixed(1) + ' mi';
  document.getElementById('sbGain').textContent   = '+' + day.gain.toLocaleString() + ' ft';
  document.getElementById('sbReqMi').textContent  = day.req_miles.toFixed(1) + ' mi';
  document.getElementById('dayLbl').textContent   = `Day ${{d}} / ${{META.n_days}}`;
  document.getElementById('daySlider').value      = d;

  const pct = Math.min(100, day.cum_req_miles / tot * 100);
  document.getElementById('progFill').style.width = pct.toFixed(1) + '%';
  document.getElementById('progPct').textContent  = pct.toFixed(1) + '%';
  document.getElementById('progMi').textContent   =
    day.cum_req_miles.toFixed(1) + ' / ' + tot.toFixed(1) + ' req mi';
  document.getElementById('completeBadge').style.display = pct >= 99.9 ? 'block' : 'none';

  // Reset step slider to show all arcs
  if (stepPlayTimer) {{
    clearInterval(stepPlayTimer); stepPlayTimer = null;
    document.getElementById('btnStepPlay').textContent = '▶ Play';
  }}
  currentStep = 1;
  const stepSl = document.getElementById('stepSlider');
  stepSl.max   = day.steps.length;
  stepSl.value = 1;
  document.getElementById('stepLbl').textContent = 'Step 1 / ' + day.steps.length;
  buildItinerary(d);
  renderForStep(d, 1);
  highlightItineraryStep(1);
}}

// ── Controls ───────────────────────────────────────────────────────────────
document.getElementById('btnHome').addEventListener('click', () => {{
  map.fitBounds(HOME_BOUNDS);
}});
document.getElementById('sidebar-toggle').addEventListener('click', () => {{
  const hidden = document.body.classList.toggle('sidebar-hidden');
  document.getElementById('sidebar-toggle').textContent = hidden ? '◀' : '▶';
  setTimeout(() => map.invalidateSize(), 260);
}});
document.getElementById('daySlider')
  .addEventListener('input', e => updateDay(+e.target.value));
document.getElementById('stepSlider').addEventListener('input', function() {{
  currentStep = +this.value;
  document.getElementById('stepLbl').textContent =
    'Step ' + currentStep + ' / ' + DAYS[currentDay-1].steps.length;
  renderForStep(currentDay, currentStep);
  highlightItineraryStep(currentStep);
}});
document.getElementById('btnStepPrev').addEventListener('click', () => {{
  const sl = document.getElementById('stepSlider');
  if (+sl.value > 1) {{ sl.value--; sl.dispatchEvent(new Event('input')); }}
}});
document.getElementById('btnStepNext').addEventListener('click', () => {{
  const sl = document.getElementById('stepSlider');
  if (+sl.value < +sl.max) {{ sl.value++; sl.dispatchEvent(new Event('input')); }}
}});
document.getElementById('btnPrev').addEventListener('click', () => {{
  if (currentDay > 1) updateDay(currentDay - 1);
}});
document.getElementById('btnNext').addEventListener('click', () => {{
  if (currentDay < META.n_days) updateDay(currentDay + 1);
}});
document.getElementById('btnPlay').addEventListener('click', function() {{
  if (playTimer) {{
    clearInterval(playTimer); playTimer = null; this.textContent = '▶ Play';
  }} else {{
    if (currentDay >= META.n_days) updateDay(1);
    this.textContent = '⏸ Pause';
    playTimer = setInterval(() => {{
      if (currentDay >= META.n_days) {{
        clearInterval(playTimer); playTimer = null;
        document.getElementById('btnPlay').textContent = '▶ Play';
      }} else updateDay(currentDay + 1);
    }}, 700);
  }}
}});
document.getElementById('btnZoom').addEventListener('click', () => {{
  const day = DAYS[currentDay - 1];
  const pts = [];
  for (const a of day.steps) {{
    const c = getCoords(a);
    if (c) for (const p of c) pts.push(p);
  }}
  if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.12));
}});
document.getElementById('btnZoomStep').addEventListener('click', () => {{
  const step = DAYS[currentDay - 1].steps[currentStep - 1];
  if (!step) return;
  const c = getCoords(step);
  if (c && c.length) map.fitBounds(L.latLngBounds(c).pad(0.15));
}});
document.getElementById('btnStepPlay').addEventListener('click', function() {{
  if (stepPlayTimer) {{
    clearInterval(stepPlayTimer); stepPlayTimer = null; this.textContent = '▶ Play';
  }} else {{
    const day = DAYS[currentDay - 1];
    if (currentStep >= day.steps.length) {{ renderForStep(currentDay, 1); currentStep = 1; }}
    this.textContent = '⏸ Pause';
    document.getElementById('stepSlider').value = currentStep;
    document.getElementById('stepLbl').textContent =
      'Step ' + currentStep + ' / ' + day.steps.length;
    stepPlayTimer = setInterval(() => {{
      const n = DAYS[currentDay - 1].steps.length;
      if (currentStep >= n) {{
        clearInterval(stepPlayTimer); stepPlayTimer = null;
        document.getElementById('btnStepPlay').textContent = '▶ Play';
      }} else {{
        currentStep++;
        const sl = document.getElementById('stepSlider');
        sl.value = currentStep;
        sl.dispatchEvent(new Event('input'));
      }}
    }}, 700);
  }}
}});
document.getElementById('togDH').addEventListener('change',
  () => updateDay(currentDay));
function updateNodeVisibility() {{
  const zoom   = map.getZoom();
  const zoomOk = zoom >= HOME_ZOOM;
  const zoomSm = zoom >= 8 && zoom < HOME_ZOOM;
  document.body.classList.toggle('zoom-small-icons', zoomSm);
  [['togIntersections', intersectionGroup],
   ['togCamping',       campingGroup],
   ['togTrailheads',    trailheadGroup]].forEach(([id, grp]) => {{
    const on = document.getElementById(id).checked && (zoomOk || zoomSm);
    if (on) grp.addTo(map); else map.removeLayer(grp);
  }});
  if (zoomOk) arrowGroup.addTo(map); else map.removeLayer(arrowGroup);
}}
map.on('zoomend', updateNodeVisibility);
['togIntersections','togCamping','togTrailheads'].forEach(id =>
  document.getElementById(id).addEventListener('change', updateNodeVisibility));
document.getElementById('togOpt').addEventListener('change', function() {{
  if (this.checked) {{ optGroup.addTo(map); bgGroup.bringToFront(); }}
  else map.removeLayer(optGroup);
}});
document.getElementById('mapwarpOpacity').addEventListener('input', function() {{
  const v = +this.value;
  mapwarperLayer.setOpacity(v / 100);
  document.getElementById('opacityVal').textContent = v + '%';
}});
document.getElementById('hillshadeOpacity').addEventListener('input', function() {{
  const v = +this.value;
  hillshadeLayer.setOpacity(v / 100);
  document.getElementById('hillshadeOpacityVal').textContent = v + '%';
}});
document.getElementById('basemapOpacity').addEventListener('input', function() {{
  const v = +this.value;
  activeBasemap.setOpacity(v / 100);
  document.getElementById('basemapOpacityVal').textContent = v + '%';
}});
document.getElementById('basemapSel').addEventListener('change', function() {{
  const opacity = +document.getElementById('basemapOpacity').value / 100;
  map.removeLayer(activeBasemap);
  activeBasemap = BASEMAPS[this.value];
  activeBasemap.addTo(map);
  activeBasemap.setOpacity(opacity);
  activeBasemap.bringToBack();
}});

// ── Init ───────────────────────────────────────────────────────────────────
updateDay(1);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Generate GSMNP interactive itinerary map")
    ap.add_argument("itinerary",
                    help="JSON itinerary export from the solver")
    ap.add_argument("--lines",  default=DEFAULT_LINES,
                    help=f"Trail LineString GeoJSON (default: {DEFAULT_LINES})")
    ap.add_argument("--points", default=DEFAULT_POINTS,
                    help=f"Node Point GeoJSON (default: {DEFAULT_POINTS})")
    ap.add_argument("--out",    default=DEFAULT_OUT,
                    help=f"Output HTML file (default: {DEFAULT_OUT})")
    args = ap.parse_args()

    print(f"Loading {args.itinerary} ...")
    itinerary = load_json(args.itinerary)

    print(f"Loading {args.lines} ...")
    lines_gj = load_json(args.lines)

    print(f"Loading {args.points} ...")
    points_gj = load_json(args.points)

    print("Building directed geometry lookup ...")
    directed_geom, named_geom = build_directed_geom(lines_gj)

    print("Building node coordinates ...")
    node_coords = build_node_coords(points_gj)

    print("Building all-nodes layer ...")
    all_nodes = build_all_nodes(points_gj)

    print("Building GEOM dict and per-day arc data ...")
    geom_dict, days_data = build_geom_and_days(itinerary, directed_geom, named_geom, node_coords)

    print("Building background required-edge layer ...")
    bg_layer = build_bg_layer(itinerary)

    print(f"  GEOM entries: {len(geom_dict)}  |  BG edges: {len(bg_layer)}  |  Nodes: {len(all_nodes)}")

    meta = {
        "circuit":              itinerary["circuit"],
        "n_days":               itinerary["n_days"],
        "total_required_miles": itinerary["total_required_miles"],
    }

    print("Fetching MapWarper bounding box ...")
    home_bounds = fetch_mapwarper_bounds(MAPWARPER_MAP_ID)

    print("Building optional segment layer ...")
    # build_optional_layer excludes by node-pair ukey, not by edge_id key
    required_ukeys = {undirected_key(seg["from"], seg["to"]) for seg in bg_layer}
    opt_layer = build_optional_layer(lines_gj, required_ukeys, geom_dict)
    print(f"  Optional segments: {len(opt_layer)}")

    print("Rendering HTML ...")
    html = render_html(meta, geom_dict, days_data, bg_layer, opt_layer, all_nodes, home_bounds)

    out_path = Path(args.out)
    out_path.write_text(html, encoding="utf-8")
    print(f"Written: {out_path}  ({len(html) // 1024} KB)")
    print(f"\nOpen in browser:  {out_path.resolve()}")


if __name__ == "__main__":
    main()
