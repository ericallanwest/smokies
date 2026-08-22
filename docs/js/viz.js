'use strict';

// ── Constants ──────────────────────────────────────────────────────────────
const HOME_BOUNDS = [[35.24, -84.10], [35.89, -82.97]];
// Data and icons are served same-origin from the Pages site itself:
// raw.githubusercontent.com is blocked by some ad/privacy filters and
// rate-limits per IP, which broke the map for some visitors.
const NPS_BASE    = 'icons/';
const LINES_URL   = 'data/lines_20250211.geojson';
const POINTS_URL  = 'data/points_20250211.geojson';
const ICON_MAP    = { BC:'campsite', SH:'shelter', CG:'trailer-site', TH:'trailhead', TI:'sign', RI:'sign' };
const RESUPPLY_ICON_URL = `${NPS_BASE}store-black-22.svg`;
// Mirrors RESUPPLY_NODES in the solver: town-access points + the two road campgrounds.
const RESUPPLY_NODES = {
  CGCAD: 'Cades Cove Campground',
  TH264: 'Standing Bear Hostel (Davenport Gap)',
  TH210: 'Cherokee',
  TH158: 'Bryson City',
  TH025: 'Fontana Village',
  RI058: 'Townsend',
  TH117: 'Gatlinburg',
  TH119: 'Gatlinburg',
  TH220: 'Cosby',
  CGSMO: 'Smokemont Campground',
};

// ── Utilities ──────────────────────────────────────────────────────────────
function fmtHM(s) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return `${h}h ${String(m).padStart(2, '0')}m`;
}

function undirectedKey(u, v) { return [u, v].sort().join('|'); }

function trailLabel(name) {
  if (!name || name.toLowerCase().endsWith('trail')) return name;
  return name + ' Trail';
}

function toLatlng(coords) {
  return coords.map(c => [+c[1].toFixed(5), +c[0].toFixed(5)]);
}

function nodeTypeLabel(nid) {
  if (nid.startsWith('BC')) return 'Backcountry Campsite';
  if (nid.startsWith('SH')) return 'Shelter';
  if (nid.startsWith('CG')) return 'Campground';
  if (nid.startsWith('TH')) return 'Trailhead';
  if (nid.startsWith('TI')) return 'Trail Intersection';
  if (nid.startsWith('RI')) return 'Road Intersection';
  return 'Node';
}

// ── Preprocessing — JS port of visualize.py Python functions ───────────────

function buildDirectedGeom(linesGJ) {
  const geom = {}, named = {};
  for (const feat of linesGJ.features) {
    const p = feat.properties;
    const s = p.Start || '', e = p.End || '';
    if (!s || !e) continue;
    const name = p.Name || '';
    const ll = toLatlng(feat.geometry.coordinates);
    const llr = [...ll].reverse();
    if (!geom[`${s}|${e}`]) geom[`${s}|${e}`] = ll;
    if (!geom[`${e}|${s}`]) geom[`${e}|${s}`] = llr;
    named[`${s}|${e}|${name}`] = ll;
    named[`${e}|${s}|${name}`] = llr;
  }
  return { geom, named };
}

function buildNodeCoords(pointsGJ) {
  const coords = {};
  for (const feat of pointsGJ.features) {
    const nid = feat.properties.id || '';
    if (!nid) continue;
    const [lon, lat] = feat.geometry.coordinates;
    coords[nid] = [+lat.toFixed(5), +lon.toFixed(5)];
  }
  return coords;
}

function buildNodeElev(pointsGJ) {
  const elev = {};
  for (const feat of pointsGJ.features) {
    const nid = feat.properties.id || '';
    if (!nid) continue;
    elev[nid] = parseFloat(feat.properties.elevation);
  }
  return elev;
}

function buildAllNodes(pointsGJ) {
  return pointsGJ.features
    .filter(f => f.properties.id)
    .map(f => {
      const p = f.properties, nid = p.id;
      const [lon, lat] = f.geometry.coordinates;
      return {
        id: nid, name: p.name || nid, type: nodeTypeLabel(nid),
        icon: ICON_MAP[nid.slice(0, 2)] || 'sign',
        coords: [+lat.toFixed(5), +lon.toFixed(5)],
      };
    });
}

function buildGeomAndDays(itinerary, directedGeom, namedGeom, nodeCoords, nodeElev, edgeGains) {
  const geomDict = {}, geomDirCache = {};
  const globalSeen = new Set();
  let cumReqMiles = 0;
  const covByDay = [], daysData = [];

  // A required edge is one the solver credits with map coverage somewhere in
  // the itinerary (its single is_deadhead=false "coverage copy").  Everything
  // else traversed is a connector (roads, optional paths).
  const requiredEids = new Set();
  for (const dayInfo of itinerary.days)
    for (const arc of dayInfo.arcs)
      if (!arc.is_deadhead && arc.edge_id != null) requiredEids.add(String(arc.edge_id));

  // Traversal categories are derived from walk order, not the solver's copy
  // bookkeeping: the solver flags whichever duplicate copy balances the Euler
  // tour, so a required trail's *first* physical traversal can carry
  // is_deadhead=true.  What the hiker cares about is first pass vs repeat.
  const traversed = new Set();

  function resolveCoords(u, v, trailName, edgeId) {
    const gkey = edgeId != null ? String(edgeId) : undirectedKey(u, v);
    if (!(gkey in geomDict)) {
      const nk  = `${u}|${v}|${trailName || ''}`;
      const nkr = `${v}|${u}|${trailName || ''}`;
      const fk  = `${u}|${v}`, rk = `${v}|${u}`;
      if (trailName && namedGeom[nk])       { geomDict[gkey] = namedGeom[nk];     geomDirCache[gkey] = u; }
      else if (trailName && namedGeom[nkr]) { geomDict[gkey] = namedGeom[nkr];    geomDirCache[gkey] = v; }
      else if (directedGeom[fk])            { geomDict[gkey] = directedGeom[fk];  geomDirCache[gkey] = u; }
      else if (directedGeom[rk])            { geomDict[gkey] = directedGeom[rk];  geomDirCache[gkey] = v; }
      else {
        const c1 = nodeCoords[u], c2 = nodeCoords[v];
        if (c1 && c2) geomDict[gkey] = [c1, c2];
        geomDirCache[gkey] = u;
      }
    }
    return { gkey, geomFwd: (geomDirCache[gkey] ?? u) === u };
  }

  for (const dayInfo of itinerary.days) {
    const steps = [];
    let dayTotalS = 0, dayNewS = 0, dayConnS = 0, dayRepS = 0;
    let dayMiles = 0, dayGain = 0, dayLoss = 0;
    let dayNewReqMiles = 0, dayConnMiles = 0, dayRepMiles = 0;

    for (const arc of dayInfo.arcs) {
      const { from: u, to: v, edge_id: eid, trail, miles, seconds, gain } = arc;
      const eidStr = eid != null ? String(eid) : null;
      const isRequired = eidStr != null && requiredEids.has(eidStr);
      const tkey = eidStr ?? undirectedKey(u, v);
      const firstPass = !traversed.has(tkey);
      traversed.add(tkey);
      const cat = !firstPass ? 'repeat' : (isRequired ? 'new' : 'connector');

      const { gkey, geomFwd } = resolveCoords(u, v, trail, eid);
      // Loss along u→v is the edge list's gain in the opposite direction;
      // fall back to gain minus net elevation change if the edge is unknown.
      const eg = eidStr != null ? edgeGains[eidStr] : null;
      const eu = nodeElev[u], ev = nodeElev[v];
      const loss = eg ? (u === eg[0] ? eg[2] : eg[1])
                      : (eu != null && ev != null ? Math.max(0, gain - (ev - eu)) : 0);
      dayTotalS += seconds; dayMiles += miles; dayGain += gain; dayLoss += loss;
      const tlabel = isRequired ? trailLabel(trail) : trail;
      steps.push({
        key: gkey, eid, geom_fwd: geomFwd, trail: tlabel,
        from: u, to: v, miles, seconds, gain, loss,
        popup: `<b>${tlabel}</b><br>${u} → ${v}<br>${miles.toFixed(2)} mi &nbsp; ${fmtHM(seconds)}<br>+${gain.toLocaleString()} ft`,
        cat,
      });
      if (cat === 'repeat') {
        dayRepS += seconds; dayRepMiles += miles;
      } else if (cat === 'connector') {
        dayConnS += seconds; dayConnMiles += miles;
      } else {
        dayNewS += seconds; dayNewReqMiles += miles;
        globalSeen.add(eid);
      }
    }
    cumReqMiles += dayNewReqMiles;
    covByDay.push(new Set(globalSeen));
    daysData.push({
      day: dayInfo.day, start_node: dayInfo.start_node, end_node: dayInfo.end_node,
      total_s: dayTotalS, new_s: dayNewS, conn_s: dayConnS, rep_s: dayRepS,
      miles:         Math.round(dayMiles       * 100) / 100,
      gain:          Math.round(dayGain),
      loss:          Math.round(dayLoss),
      req_miles:     Math.round(dayNewReqMiles * 100) / 100,
      conn_miles:    Math.round(dayConnMiles   * 100) / 100,
      rep_miles:     Math.round(dayRepMiles    * 100) / 100,
      cum_req_miles: Math.round(cumReqMiles    * 100) / 100,
      start_coords: nodeCoords[dayInfo.start_node] || null,
      end_coords:   nodeCoords[dayInfo.end_node]   || null,
      steps,
    });
  }
  return { geomDict, daysData, covByDay };
}

function buildBgLayer(itinerary) {
  const seen = new Set(), bg = [];
  for (const dayInfo of itinerary.days) {
    for (const arc of dayInfo.arcs) {
      if (arc.is_deadhead || arc.edge_id == null || seen.has(arc.edge_id)) continue;
      seen.add(arc.edge_id);
      bg.push({
        key: String(arc.edge_id), eid: arc.edge_id,
        trail: trailLabel(arc.trail),
        from: arc.from, to: arc.to,
        miles: arc.miles, gain: arc.gain, seconds: arc.seconds,
      });
    }
  }
  return bg;
}

function buildOptionalLayer(linesGJ, bgLayer, geomDict) {
  const reqUkeys = new Set(bgLayer.map(s => undirectedKey(s.from, s.to)));
  const seen = new Set(), opt = [];
  for (const feat of linesGJ.features) {
    const p = feat.properties;
    const s = p.Start || '', e = p.End || '';
    if (!s || !e) continue;
    const ukey = undirectedKey(s, e);
    if (reqUkeys.has(ukey) || seen.has(ukey)) continue;
    seen.add(ukey);
    if (!(ukey in geomDict)) geomDict[ukey] = toLatlng(feat.geometry.coordinates);
    opt.push({
      key: ukey, trail: p.Name || '', from: s, to: e,
      miles: parseFloat(p.Miles || 0), gain: parseInt(p.Gain || 0, 10),
    });
  }
  return opt;
}

// ── Module-level state ─────────────────────────────────────────────────────
let map;
let META, GEOM, DAYS, BG, OPT, ALL_NODES, cumCov;
let currentDay = 1, currentStep = 0;
let playTimer = null, stepPlayTimer = null;
let startMarker = null, endMarker = null;
let selectedBgPoly = null, selectedOptPoly = null;
const NODE_NAME = {};

// Leaflet layer groups (created once, contents cleared on preset change)
const bgGroup           = L.layerGroup();
const covGroup          = L.layerGroup();
const reqGroup          = L.layerGroup();   // today: first pass of required trails
const connGroup         = L.layerGroup();   // today: first pass of connectors (roads, optional paths)
const dhGroup           = L.layerGroup();   // today: repeats (edges already traversed)
const arrowGroup        = L.layerGroup();
const optGroup          = L.layerGroup();
const intersectionGroup = L.layerGroup();
const campingGroup      = L.layerGroup();
const trailheadGroup    = L.layerGroup();
const resupplyGroup     = L.layerGroup();

// Map-line grays per theme: the dark "hiked" lines vanish on a dark basemap,
// so both grays flip with the UI theme (see applyTheme).
const TRAIL_THEMES = {
  light: { bg:'#999',    cov:'#333333' },
  dark:  { bg:'#6b6b78', cov:'#d5d5dc' },
};
let trailTheme = TRAIL_THEMES.light;

const BG_NORMAL  = { color:trailTheme.bg, weight:5, opacity:0.75 };
const BG_SEL     = { color:'#FFD700',  weight:7, opacity:1    };
const OPT_NORMAL = { color:'#f08080',  weight:4, opacity:0.65, dashArray:'4,6' };
const OPT_SEL    = { color:'#FFD700',  weight:7, opacity:1,    dashArray:null  };

// ── Leaflet helpers ────────────────────────────────────────────────────────
const _iconCache = {};
function npsIcon(name) {
  if (!_iconCache[name])
    _iconCache[name] = L.icon({
      iconUrl: `${NPS_BASE}${name}-black-22.svg`,
      iconSize:[22,22], iconAnchor:[11,11], popupAnchor:[0,-12], className:'nps-icon',
    });
  return _iconCache[name];
}

function resupplyIcon() {
  if (!_iconCache['__resupply'])
    _iconCache['__resupply'] = L.icon({
      iconUrl: RESUPPLY_ICON_URL,
      iconSize:[22,22], iconAnchor:[11,11], popupAnchor:[0,-12], className:'nps-icon',
    });
  return _iconCache['__resupply'];
}

function triIcon(color, up) {
  const pts = up ? '9,2 17,16 1,16' : '1,2 17,2 9,16';
  return L.divIcon({
    html: `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18"><polygon points="${pts}" fill="${color}" stroke="#fff" stroke-width="2" stroke-linejoin="round"/></svg>`,
    className:'', iconSize:[18,18], iconAnchor:[9,9],
  });
}

function getCoords(a) {
  const raw = GEOM[a.key];
  if (!raw) return null;
  return a.geom_fwd ? raw : [...raw].reverse();
}

function addPolyDecorated(lineGrp, arrowGrp, a, lineOpts, arrowColor) {
  const coords = getCoords(a);
  if (!coords || coords.length < 2) return;
  const pl = L.polyline(coords, lineOpts);
  if (a.popup) pl.bindPopup(a.popup);
  if (a.trail) pl.bindTooltip(a.trail, { sticky:true, opacity:0.85 });
  pl.addTo(lineGrp);
  const mkArrow = (color, wt) => L.polylineDecorator(pl, {
    patterns: [{ offset:'10%', repeat:'120px', symbol: L.Symbol.arrowHead({
      pixelSize:12, polygon:false,
      pathOptions:{ color, weight:wt, opacity:0.9, fillOpacity:0 },
    })}],
  });
  mkArrow('#fff', 6).addTo(arrowGrp);
  mkArrow(arrowColor, 2).addTo(arrowGrp);
}

function addPoly(group, coords, opts, popup, tooltip) {
  if (!coords || coords.length < 2) return;
  const pl = L.polyline(coords, opts);
  if (popup)   pl.bindPopup(popup);
  if (tooltip) pl.bindTooltip(tooltip, { sticky:true, opacity:0.85 });
  pl.addTo(group);
}

// ── Rendering ──────────────────────────────────────────────────────────────
const showRep  = () => document.getElementById('togDH').checked;
const showConn = () => document.getElementById('togConn').checked;
// Connectors and repeats share the red colorway — neither adds new
// map-completion mileage.  Connectors draw solid, repeats dashed.
const CAT_COLOR = { new:'#f7882f', connector:'#c0392b', repeat:'#c0392b' };
function nodeName(id) { return NODE_NAME[id] || id; }

function buildItinerary(d) {
  const day = DAYS[d - 1];
  document.getElementById('itinerary').innerHTML = day.steps.map((s, i) => {
    const dot   = CAT_COLOR[s.cat];
    const extra = s.cat === 'repeat'    ? ' <span style="color:var(--accent);font-size:10px">(repeat)</span>'
                : s.cat === 'connector' ? ' <span style="color:var(--accent);font-size:10px">(connector)</span>'
                : '';
    return `<div class="itin-step" data-step="${i+1}" style="padding:3px 4px 3px 6px;border-radius:3px;cursor:pointer;border-left:3px solid transparent">
      <span style="color:${dot};font-weight:700">${i+1}.</span>
      <b>${s.trail}</b>${extra}<br>
      <span style="color:var(--muted);padding-left:12px">
        ${nodeName(s.from)} → ${nodeName(s.to)}<br>
        ${s.miles.toFixed(2)} mi &nbsp; ${fmtHM(s.seconds)} &nbsp; ${s.gain} ft ↑ / ${s.loss} ft ↓
      </span>
    </div>`;
  }).join('');
  document.getElementById('itinerary').querySelectorAll('.itin-step').forEach(row => {
    row.addEventListener('click', () => {
      const sl = document.getElementById('stepSlider');
      sl.value = row.dataset.step;
      sl.dispatchEvent(new Event('input'));
    });
  });
}

function highlightItineraryStep(step) {
  const n = DAYS[currentDay - 1].steps.length;
  const isFull = step >= n;
  document.querySelectorAll('.itin-step').forEach((row, i) => {
    const active = !isFull && (i + 1 === step);
    row.style.background = active ? 'var(--active-bg)' : '';
    row.style.borderLeft = active ? '3px solid #FFD700' : '3px solid transparent';
  });
  if (!isFull && step > 0) {
    const row = document.querySelector(`.itin-step[data-step="${step}"]`);
    if (row) row.scrollIntoView({ block:'nearest' });
  }
}

function stepGroup(cat) {
  return cat === 'repeat' ? dhGroup : cat === 'connector' ? connGroup : reqGroup;
}

function stepStyle(cat) {
  if (cat === 'repeat')    return { color:'#c0392b', weight:5, opacity:0.9, dashArray:'6,5' };
  if (cat === 'connector') return { color:'#c0392b', weight:5, opacity:0.9 };
  return { color:'#f7882f', weight:5, opacity:1 };
}

function stepVisible(cat) {
  if (cat === 'repeat')    return showRep();
  if (cat === 'connector') return showConn();
  return true;
}

function renderForStep(d, step) {
  const day = DAYS[d - 1];
  [covGroup, reqGroup, connGroup, dhGroup, arrowGroup].forEach(g => g.clearLayers());

  for (const seg of BG) {
    if (!cumCov[d - 1].has(seg.eid)) continue;
    addPoly(covGroup, GEOM[seg.key], { color:trailTheme.cov, weight:5, opacity:0.85 },
      `<b>${seg.trail}</b><br>${seg.from} ↔ ${seg.to}<br>${seg.miles.toFixed(2)} mi &nbsp; ${fmtHM(seg.seconds)}<br>+${seg.gain.toLocaleString()} ft`,
      seg.trail);
  }

  const count = (step >= day.steps.length) ? day.steps.length : step;
  for (let i = 0; i < count; i++) {
    const s = day.steps[i], isLast = (i === count - 1);
    let lineOpts, arrowColor;
    if (isLast) { lineOpts = { color:'#FFD700', weight:7, opacity:1 }; arrowColor = '#FFD700'; }
    else {
      if (!stepVisible(s.cat)) continue;
      lineOpts = stepStyle(s.cat); arrowColor = CAT_COLOR[s.cat];
    }
    addPolyDecorated(stepGroup(s.cat), arrowGroup, s, lineOpts, arrowColor);
  }
}

function updateDay(d) {
  currentDay = d;
  const day = DAYS[d - 1];
  const tot = META.total_required_miles;

  [covGroup, reqGroup, connGroup, dhGroup, arrowGroup].forEach(g => g.clearLayers());
  if (startMarker) { startMarker.remove(); startMarker = null; }
  if (endMarker)   { endMarker.remove();   endMarker   = null; }
  if (selectedBgPoly) { selectedBgPoly.setStyle(BG_NORMAL); selectedBgPoly = null; }

  for (const seg of BG) {
    if (!cumCov[d - 1].has(seg.eid)) continue;
    addPoly(covGroup, GEOM[seg.key], { color:trailTheme.cov, weight:5, opacity:0.85 },
      `<b>${seg.trail}</b><br>${seg.from} ↔ ${seg.to}<br>${seg.miles.toFixed(2)} mi &nbsp; ${fmtHM(seg.seconds)}<br>+${seg.gain.toLocaleString()} ft`,
      seg.trail);
  }
  for (const a of day.steps)
    if (stepVisible(a.cat))
      addPolyDecorated(stepGroup(a.cat), arrowGroup, a, stepStyle(a.cat), CAT_COLOR[a.cat]);

  if (day.start_coords)
    startMarker = L.marker(day.start_coords, { icon:triIcon('#27ae60', true), zIndexOffset:1000 })
      .bindPopup(`<b>Day ${d} Start</b><br>${day.start_node}`).addTo(map);
  if (day.end_coords)
    endMarker = L.marker(day.end_coords, { icon:triIcon('#c0392b', false), zIndexOffset:1001 })
      .bindPopup(`<b>Day ${d} Camp</b><br>${day.end_node}`).addTo(map);

  // Sidebar stats
  const rsStop = META.resupply_plan?.find(s => s.day === d);
  document.getElementById('sbDay').textContent   = `Day ${d} of ${META.n_days}`;
  document.getElementById('sbRoute').textContent =
    `${nodeName(day.start_node)} → ${nodeName(day.end_node)}` +
    (rsStop ? ` · resupply: ${rsStop.name}` : '');
  document.querySelectorAll('.rs-stop').forEach(row =>
    row.classList.toggle('active', +row.dataset.day === d));
  document.getElementById('sbTotal').textContent  = `${day.miles.toFixed(1)} mi / ${fmtHM(day.total_s)}`;
  document.getElementById('sbNew').textContent    = `${day.req_miles.toFixed(1)} mi / ${fmtHM(day.new_s)}`;
  document.getElementById('sbRepeat').textContent = `${day.rep_miles.toFixed(1)} mi / ${fmtHM(day.rep_s)}`;
  document.getElementById('sbConn').textContent   = `${day.conn_miles.toFixed(1)} mi / ${fmtHM(day.conn_s)}`;
  document.getElementById('sbElev').textContent   = `${day.gain} ft ↑ / ${day.loss} ft ↓`;
  document.getElementById('dayLbl').textContent  = `Day ${d} / ${META.n_days}`;
  document.getElementById('daySlider').value     = d;

  const pct = Math.min(100, day.cum_req_miles / tot * 100);
  document.getElementById('progFill').style.width = pct.toFixed(1) + '%';
  document.getElementById('progPct').textContent  = pct.toFixed(1) + '%';
  document.getElementById('progMi').textContent   = `${day.cum_req_miles.toFixed(1)} / ${tot.toFixed(1)} req mi`;
  document.getElementById('completeBadge').style.display = pct >= 99.9 ? 'block' : 'none';

  // Reset step slider
  if (stepPlayTimer) { clearInterval(stepPlayTimer); stepPlayTimer = null; document.getElementById('btnStepPlay').textContent = '▶ Play'; }
  currentStep = 1;
  const stepSl = document.getElementById('stepSlider');
  stepSl.max = day.steps.length; stepSl.value = 1;
  document.getElementById('stepLbl').textContent = `Step 1 / ${day.steps.length}`;
  buildItinerary(d);
  renderForStep(d, 1);
  highlightItineraryStep(1);
}

// ── Basemaps ───────────────────────────────────────────────────────────────
const _BingLayer = L.TileLayer.extend({
  getTileUrl(coords) {
    let q = '';
    for (let i = coords.z; i > 0; i--) {
      let d = 0, m = 1 << (i - 1);
      if (coords.x & m) d++;
      if (coords.y & m) d += 2;
      q += d;
    }
    return `https://ecn.t3.tiles.virtualearth.net/tiles/a${q}.jpeg?g=1`;
  },
});

const BASEMAPS = {
  'OSM Grayscale': L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    { attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', maxZoom:19, zIndex:1, className:'grayscale-layer' }),
  'OSM Color': L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    { attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', maxZoom:19, zIndex:1 }),
  'CartoDB Light': L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
    { attribution:'&copy; OpenStreetMap contributors &copy; CARTO', maxZoom:19, zIndex:1 }),
  'CartoDB Dark': L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
    { attribution:'&copy; OpenStreetMap contributors &copy; CARTO', maxZoom:19, zIndex:1 }),
  'Google Maps': L.tileLayer('https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}',
    { attribution:'&copy; Google', maxZoom:20, zIndex:1 }),
  'Bing Aerial': new _BingLayer('', { attribution:'&copy; Microsoft Bing', maxZoom:19, zIndex:1 }),
  'ESRI World Topo': L.tileLayer('https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',
    { attribution:'Tiles &copy; Esri', maxZoom:19, zIndex:1 }),
};

// ── initViz: rebuild all data layers for a new preset ─────────────────────
function initViz(meta, geomDict, daysData, bgLayer, optLayer, allNodes, cov) {
  META = meta; GEOM = geomDict; DAYS = daysData; BG = bgLayer; OPT = optLayer;
  ALL_NODES = allNodes; cumCov = cov;

  // Rebuild node name lookup
  for (const k of Object.keys(NODE_NAME)) delete NODE_NAME[k];
  for (const n of allNodes) NODE_NAME[n.id] = n.name;

  // Clear all data layers
  [bgGroup, covGroup, reqGroup, connGroup, dhGroup, arrowGroup, optGroup,
   intersectionGroup, campingGroup, trailheadGroup, resupplyGroup].forEach(g => g.clearLayers());
  if (startMarker) { startMarker.remove(); startMarker = null; }
  if (endMarker)   { endMarker.remove();   endMarker   = null; }

  // Available connector network (non-required segments) — added before the
  // required edges so its paths render beneath them
  for (const seg of OPT) {
    const coords = GEOM[seg.key];
    if (!coords || coords.length < 2) continue;
    const popup = `<b>${seg.trail}</b><br>${seg.from} ↔ ${seg.to}<br>${seg.miles.toFixed(2)} mi<br>+${seg.gain.toLocaleString()} ft`;
    const poly = L.polyline(coords, { ...OPT_NORMAL })
      .bindTooltip(seg.trail, { sticky:true, opacity:0.85 })
      .bindPopup(popup);
    poly.on('click', e => {
      L.DomEvent.stopPropagation(e);
      if (selectedOptPoly) selectedOptPoly.setStyle(OPT_NORMAL);
      selectedOptPoly = poly; poly.setStyle(OPT_SEL);
    });
    poly.addTo(optGroup);
  }

  // Background required edges (all, gray — coverage highlighted per day)
  for (const seg of BG) {
    const coords = GEOM[seg.key];
    if (!coords || coords.length < 2) continue;
    const popup = `<b>${seg.trail}</b><br>${seg.from} ↔ ${seg.to}<br>${seg.miles.toFixed(2)} mi &nbsp; ${fmtHM(seg.seconds)}<br>+${seg.gain.toLocaleString()} ft`;
    const poly = L.polyline(coords, { ...BG_NORMAL })
      .bindTooltip(seg.trail, { sticky:true, opacity:0.85 })
      .bindPopup(popup);
    poly.on('click', e => {
      L.DomEvent.stopPropagation(e);
      if (selectedBgPoly) selectedBgPoly.setStyle(BG_NORMAL);
      selectedBgPoly = poly; poly.setStyle(BG_SEL);
    });
    poly.addTo(bgGroup);
  }

  // NPS icon markers by node type
  for (const n of allNodes) {
    if (!n.coords) continue;
    const pfx = n.id.slice(0, 2);
    const grp = ['TI','RI'].includes(pfx) ? intersectionGroup
              : ['BC','SH','CG'].includes(pfx) ? campingGroup
              : trailheadGroup;
    L.marker(n.coords, { icon: npsIcon(n.icon) })
      .bindPopup(`<b>${n.name}</b><br>${n.type}<br><small>${n.id}</small>`)
      .addTo(grp);
    if (n.id in RESUPPLY_NODES)
      L.marker(n.coords, { icon: resupplyIcon(), zIndexOffset: 500 })
        .bindPopup(`<b>${n.name}</b><br>Resupply point — ${RESUPPLY_NODES[n.id]}<br><small>${n.id}</small>`)
        .addTo(resupplyGroup);
  }

  // Update controls
  const sl = document.getElementById('daySlider');
  sl.max = meta.n_days; sl.value = 1;

  // Update left sidebar info panel
  document.getElementById('presetInfo').innerHTML =
    `<div class="info-row"><span>Circuit</span>        <span><b>${meta.circuit}</b></span></div>` +
    `<div class="info-row"><span>Days</span>           <span><b>${meta.n_days}</b></span></div>` +
    `<div class="info-row"><span>Required miles</span> <span><b>${meta.total_required_miles.toFixed(1)}</b></span></div>`;

  renderResupplyPlan(meta);
  updateDay(1);
}

// ── Resupply plan panel + day-slider tick marks ────────────────────────────
function renderResupplyPlan(meta) {
  const panel = document.getElementById('resupplyPlan');
  const ticks = document.getElementById('dayTicks');
  ticks.innerHTML = '';
  if (!meta.resupply_plan || !meta.resupply_plan.length) {
    panel.style.display = 'none';
    panel.innerHTML = '';
    return;
  }

  panel.innerHTML =
    `<div class="rs-title">Resupply plan · ${meta.resupply_plan.length} stops</div>` +
    meta.resupply_plan.map(s =>
      `<div class="rs-stop" data-day="${s.day}"
            title="${s.days_since_last - 1} full day(s) hiked without resupply before this stop — click to view day">
         <span class="rs-day">Day ${s.day}</span>
         <span class="rs-name">${s.name}${s.in_park ? ' <span class="rs-park">in park</span>' : ''}</span>
       </div>`).join('') +
    `<div class="rs-note">Starts fully supplied; never hikes more than
      ${meta.max_days_between_resupply} full days without restocking
      (stops mid-day, so stop day numbers can be
      ${meta.max_days_between_resupply + 1} apart). Stops without the
      <span class="rs-park">in park</span> tag need extra town-access
      miles not counted in the itinerary.</div>`;
  panel.style.display = 'block';
  panel.querySelectorAll('.rs-stop').forEach(row =>
    row.addEventListener('click', () => updateDay(+row.dataset.day)));

  for (const s of meta.resupply_plan) {
    const f = meta.n_days > 1 ? (s.day - 1) / (meta.n_days - 1) : 0;
    const t = document.createElement('div');
    t.className = 'day-tick' + (s.in_park ? ' in-park' : '');
    t.style.left = `calc((100% - 14px) * ${f.toFixed(4)} + 7px)`;
    t.title = `Day ${s.day}: resupply at ${s.name}`;
    t.addEventListener('click', () => updateDay(s.day));
    ticks.appendChild(t);
  }
}

// ── Data loading ───────────────────────────────────────────────────────────
let _linesGJ = null, _pointsGJ = null, _edgeGains = null;

function showLoading(on) {
  document.getElementById('loading').classList.toggle('visible', on);
}

async function ensureBaseData() {
  if (_linesGJ) return;
  [_linesGJ, _pointsGJ, _edgeGains] = await Promise.all([
    fetch(LINES_URL).then(r  => { if (!r.ok)  throw new Error('lines GeoJSON failed');  return r.json(); }),
    fetch(POINTS_URL).then(r => { if (!r.ok)  throw new Error('points GeoJSON failed'); return r.json(); }),
    fetch('data/edge_gains.json').then(r => { if (!r.ok) throw new Error('edge gains failed'); return r.json(); }),
  ]);
}

let _loadSeq = 0;   // last-click-wins: stale loads abandon before rendering

// Render an itinerary object (a preset file's contents, or the identical
// schema returned by the solve backend's open/closed results).
async function renderItinerary(itinerary) {
  await ensureBaseData();
  const { geom, named }               = buildDirectedGeom(_linesGJ);
  const nodeCoords                     = buildNodeCoords(_pointsGJ);
  const nodeElev                       = buildNodeElev(_pointsGJ);
  const allNodes                       = buildAllNodes(_pointsGJ);
  const { geomDict, daysData, covByDay } = buildGeomAndDays(itinerary, geom, named, nodeCoords, nodeElev, _edgeGains);
  const bgLayer                        = buildBgLayer(itinerary);
  const optLayer                       = buildOptionalLayer(_linesGJ, bgLayer, geomDict);
  const meta = {
    circuit: itinerary.circuit,
    n_days:  itinerary.n_days,
    total_required_miles: itinerary.total_required_miles,
    // Present only on resupply presets: minimal stop schedule computed by
    // the solver (hiker starts fully supplied, stops as late as the window
    // allows).  [{day, node, name, in_park, days_since_last}]
    resupply_plan: itinerary.resupply_plan || null,
    max_days_between_resupply: itinerary.max_days_between_resupply || null,
  };
  initViz(meta, geomDict, daysData, bgLayer, optLayer, allNodes, covByDay);
}

async function loadPreset(filename) {
  const seq = ++_loadSeq;
  showLoading(true);
  const errEl = document.getElementById('presetError');
  errEl.style.display = 'none';
  try {
    await ensureBaseData();
    const itinerary = await fetch(`data/${filename}`)
      .then(r => { if (!r.ok) throw new Error(`${filename} not found — has it been pre-computed?`); return r.json(); });
    if (seq !== _loadSeq) return;   // superseded by a newer selection
    await renderItinerary(itinerary);
    _shownPace = { ...PACE_DEFAULT };   // presets are the published pace
    renderPace();
  } catch (err) {
    if (seq !== _loadSeq) return;
    errEl.textContent    = err.message;
    errEl.style.display  = 'block';
  } finally {
    if (seq === _loadSeq) showLoading(false);
  }
}

function currentPresetFile() {
  const max  = document.querySelector('input[name="max_day"]:checked')?.value  ?? '12';
  const cir  = document.querySelector('input[name="circuit"]:checked')?.value  ?? 'open';
  const rs   = document.querySelector('input[name="resupply"]:checked')?.value ?? 'none';
  const town = document.querySelector('input[name="town"]:checked')?.value     ?? 'no';
  return `preset_${cir}_${max}h${rs === 'none' ? '' : `_r${rs}`}${town === 'yes' ? '_town' : ''}.json`;
}

// ── Custom solve (beta): on-demand solving via the backend service ─────────
// Hidden unless a backend URL is configured: open the site once with
// ?backend=http://localhost:8080 (persisted in localStorage; ?backend=off
// forgets it).  See backend/README.md.
// ── Hiking pace ───────────────────────────────────────────────────────────
// Tobler's hiking function, W = v0 * exp(-k * |slope - peak|).  These three
// numbers are solver input, not display settings: k decides how much a grade
// costs, which decides which direction of each trail is cheap, which decides
// the whole circuit.  So the panel builds a new itinerary rather than
// re-labelling the current one, and says so.
//
// Every control snaps to a grid.  That is what makes a pace reproducible --
// the solver is deterministic, so "k = 3.5" is the same itinerary for everyone
// -- and it keeps the backend's result cache useful, which a continuous slider
// would defeat.
const PACE_DEFAULT = { v0: 6000, k: 3.5, peak: -0.05 };

// The pace the itinerary on screen was actually built at, which is not always
// the pace the sliders show.  Keeping them distinct is the whole point: it
// lets the panel say "this is what you are looking at" instead of leaving a
// stale slider implying an itinerary that was never solved.
let _shownPace = { ...PACE_DEFAULT };
const paceEq = (a, b) => a.v0 === b.v0 && a.k === b.k && a.peak === b.peak;

function paceFromUI() {
  return {
    v0:   +document.getElementById('paceV0').value,
    k:   +(+document.getElementById('paceK').value).toFixed(1),
    peak: +(+document.getElementById('pacePeak').value).toFixed(2),
  };
}

function paceIsDefault(p) {
  return p.v0 === PACE_DEFAULT.v0 && p.k === PACE_DEFAULT.k
      && p.peak === PACE_DEFAULT.peak;
}

function setPaceUI(p) {
  document.getElementById('paceV0').value   = p.v0;
  document.getElementById('paceK').value    = p.k;
  document.getElementById('pacePeak').value = p.peak;
  renderPace();
}

// Speed on level ground, which is what v0 means to a hiker -- v0 itself is the
// peak of the curve and sits slightly downhill, so quoting it would overstate.
function levelSpeedMph(p) {
  return p.v0 * Math.exp(-p.k * Math.abs(0 - p.peak)) / 1609.344;
}

function renderPace() {
  const p = paceFromUI();
  document.getElementById('paceV0Out').textContent   = levelSpeedMph(p).toFixed(1) + ' mph';
  document.getElementById('paceKOut').textContent    = p.k.toFixed(1);
  document.getElementById('pacePeakOut').textContent = (p.peak * 100).toFixed(0) + '%';

  document.querySelectorAll('#paceTiers button').forEach(b => b.classList.toggle('on',
    +b.dataset.v0 === p.v0 && +b.dataset.k === p.k && +b.dataset.peak === p.peak));

  const note = document.getElementById('paceNote');
  const stale = !paceEq(p, _shownPace);
  note.classList.toggle('dirty', stale);
  note.textContent = stale
    ? 'A different pace makes a different itinerary, not the same one re-timed. '
      + 'Build to re-solve the circuit at this pace (about 10–25 s).'
    : paceIsDefault(p)
      ? 'Published pace — this itinerary is pre-solved at these settings.'
      : 'This itinerary was built at this pace.';
  return p;
}

// A pace belongs in the URL: it is reproducible, so a link to one is a link to
// exactly one itinerary.
function paceToQuery(p) {
  return paceIsDefault(p) ? '' :
    `&v0=${p.v0}&k=${p.k}&peak=${p.peak}`;
}

function backendUrl() {
  return localStorage.getItem('smokiesBackend');
}

function setSolveProgress(pct, label) {
  const box = document.getElementById('solveProg');
  box.style.display = pct === null ? 'none' : 'block';
  if (pct !== null) {
    document.getElementById('solveProgFill').style.width = pct + '%';
    document.getElementById('solveProgLabel').textContent = label || '';
  }
}

async function solveCustom() {
  const seq = ++_loadSeq;
  const errEl = document.getElementById('presetError');
  errEl.style.display = 'none';
  showLoading(true);
  setSolveProgress(0, 'Contacting solver…');
  try {
    const hiked = document.getElementById('hikedInput').value
      .split(/[\s,]+/).filter(Boolean);
    const rsVal = document.querySelector('input[name="resupply"]:checked')?.value;
    const body = {
      max_hours: +(document.querySelector('input[name="max_day"]:checked')?.value ?? 12),
      max_resupply_days: rsVal && rsVal !== 'none' ? +rsVal : null,
      town_nights: document.querySelector('input[name="town"]:checked')?.value === 'yes',
      hiked,
      time_budget: 45,
    };
    const pace = paceFromUI();
    if (!paceIsDefault(pace)) {
      body.tobler_v0 = pace.v0;
      body.tobler_k = pace.k;
      body.tobler_peak = pace.peak;
    }
    const resp = await fetch(backendUrl().replace(/\/+$/, '') + '/solve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`solver backend HTTP ${resp.status}`);

    // The body is streamed NDJSON: progress lines, then one result/error.
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '', result = null;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const ln of lines) {
        if (!ln.trim()) continue;
        const ev = JSON.parse(ln);
        if (ev.type === 'progress')     setSolveProgress(ev.pct, ev.label);
        else if (ev.type === 'result')  result = ev;
        else if (ev.type === 'error')   throw new Error(ev.message);
      }
    }
    if (seq !== _loadSeq) return;   // superseded by a preset click meanwhile
    if (!result) throw new Error('solver stream ended without a result');
    if (result.map_complete) throw new Error('Map already complete — nothing left to hike!');
    const cir = document.querySelector('input[name="circuit"]:checked')?.value ?? 'open';
    const itinerary = result[cir] || result.open || result.closed;
    if (!itinerary) throw new Error('solver found no valid itinerary for these settings');
    await renderItinerary(itinerary);
    _shownPace = pace;
    renderPace();
  } catch (err) {
    if (seq === _loadSeq) {
      errEl.textContent   = err.message;
      errEl.style.display = 'block';
    }
  } finally {
    if (seq === _loadSeq) { setSolveProgress(null); showLoading(false); }
  }
}

// ── Node layer visibility (zoom-dependent) ─────────────────────────────────
function updateNodeVisibility() {
  const zoom = map.getZoom(), zoomOk = zoom >= 10, zoomSm = zoom >= 8 && zoom < 10;
  document.body.classList.toggle('zoom-small-icons', zoomSm);
  [['togIntersections', intersectionGroup],
   ['togCamping',       campingGroup],
   ['togTrailheads',    trailheadGroup],
   ['togResupply',      resupplyGroup]].forEach(([id, grp]) => {
    if (document.getElementById(id).checked && (zoomOk || zoomSm)) grp.addTo(map);
    else map.removeLayer(grp);
  });
  if (zoomOk) arrowGroup.addTo(map); else map.removeLayer(arrowGroup);
}

// ── App init (runs once on DOMContentLoaded) ───────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Create Leaflet map
  map = L.map('map');
  map.fitBounds(HOME_BOUNDS);

  // Basemap (default: OSM Grayscale at low opacity)
  let activeBasemap = BASEMAPS['OSM Grayscale'].addTo(map);
  activeBasemap.setOpacity(0.15);

  // Fixed overlays
  const mapwarperLayer = L.tileLayer(
    'https://mapwarper.net/maps/tile/88180/{z}/{x}/{y}.png',
    { attribution:'Trail map &copy; NPS via <a href="https://mapwarper.net/maps/88180">MapWarper</a>', maxZoom:18, opacity:0.4, zIndex:3 }
  ).addTo(map);

  const hillshadeLayer = L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/Elevation/World_Hillshade/MapServer/tile/{z}/{y}/{x}',
    { attribution:'Hillshade &copy; Esri', maxZoom:16, opacity:0.15, zIndex:2 }
  ).addTo(map);

  // Persistent layer groups (optGroup rides the Connectors toggle, on by default)
  [optGroup, bgGroup, covGroup, connGroup, dhGroup, reqGroup, arrowGroup].forEach(g => g.addTo(map));

  map.on('click', () => {
    if (selectedBgPoly)  { selectedBgPoly.setStyle(BG_NORMAL);   selectedBgPoly  = null; }
    if (selectedOptPoly) { selectedOptPoly.setStyle(OPT_NORMAL); selectedOptPoly = null; }
  });
  map.on('zoomend', updateNodeVisibility);

  // ── Sidebar toggles ──────────────────────────────────────────────────────
  document.getElementById('left-toggle').addEventListener('click', () => {
    const hidden = document.body.classList.toggle('left-hidden');
    document.getElementById('left-toggle').textContent = hidden ? '▶' : '◀';
    setTimeout(() => map.invalidateSize(), 260);
  });
  document.getElementById('sidebar-toggle').addEventListener('click', () => {
    const hidden = document.body.classList.toggle('sidebar-hidden');
    document.getElementById('sidebar-toggle').textContent = hidden ? '◀' : '▶';
    setTimeout(() => map.invalidateSize(), 260);
  });

  // ── Day / step controls ──────────────────────────────────────────────────
  document.getElementById('btnHome').addEventListener('click', () => map.fitBounds(HOME_BOUNDS));
  document.getElementById('btnPrev').addEventListener('click', () => { if (currentDay > 1) updateDay(currentDay - 1); });
  document.getElementById('btnNext').addEventListener('click', () => { if (DAYS && currentDay < META.n_days) updateDay(currentDay + 1); });
  document.getElementById('daySlider').addEventListener('input', e => updateDay(+e.target.value));

  document.getElementById('btnPlay').addEventListener('click', function() {
    if (playTimer) { clearInterval(playTimer); playTimer = null; this.textContent = '▶ Play'; return; }
    if (currentDay >= META.n_days) updateDay(1);
    this.textContent = '⏸ Pause';
    playTimer = setInterval(() => {
      if (currentDay >= META.n_days) { clearInterval(playTimer); playTimer = null; document.getElementById('btnPlay').textContent = '▶ Play'; }
      else updateDay(currentDay + 1);
    }, 700);
  });

  document.getElementById('btnZoom').addEventListener('click', () => {
    const pts = [];
    for (const a of DAYS[currentDay - 1].steps) { const c = getCoords(a); if (c) pts.push(...c); }
    if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.12));
  });

  document.getElementById('stepSlider').addEventListener('input', function() {
    currentStep = +this.value;
    document.getElementById('stepLbl').textContent = `Step ${currentStep} / ${DAYS[currentDay-1].steps.length}`;
    renderForStep(currentDay, currentStep);
    highlightItineraryStep(currentStep);
  });
  document.getElementById('btnStepPrev').addEventListener('click', () => {
    const sl = document.getElementById('stepSlider');
    if (+sl.value > 1) { sl.value--; sl.dispatchEvent(new Event('input')); }
  });
  document.getElementById('btnStepNext').addEventListener('click', () => {
    const sl = document.getElementById('stepSlider');
    if (+sl.value < +sl.max) { sl.value++; sl.dispatchEvent(new Event('input')); }
  });
  document.getElementById('btnZoomStep').addEventListener('click', () => {
    const step = DAYS[currentDay - 1].steps[currentStep - 1];
    if (!step) return;
    const c = getCoords(step);
    if (c && c.length) map.fitBounds(L.latLngBounds(c).pad(0.15));
  });

  document.getElementById('btnStepPlay').addEventListener('click', function() {
    if (stepPlayTimer) { clearInterval(stepPlayTimer); stepPlayTimer = null; this.textContent = '▶ Play'; return; }
    const day = DAYS[currentDay - 1];
    if (currentStep >= day.steps.length) { renderForStep(currentDay, 1); currentStep = 1; }
    this.textContent = '⏸ Pause';
    document.getElementById('stepSlider').value = currentStep;
    document.getElementById('stepLbl').textContent = `Step ${currentStep} / ${day.steps.length}`;
    stepPlayTimer = setInterval(() => {
      if (currentStep >= DAYS[currentDay - 1].steps.length) {
        clearInterval(stepPlayTimer); stepPlayTimer = null;
        document.getElementById('btnStepPlay').textContent = '▶ Play';
      } else {
        currentStep++;
        const sl = document.getElementById('stepSlider');
        sl.value = currentStep; sl.dispatchEvent(new Event('input'));
      }
    }, 700);
  });

  // ── Toggle controls ──────────────────────────────────────────────────────
  document.getElementById('togDH').addEventListener('change', () => updateDay(currentDay));
  document.getElementById('togConn').addEventListener('change', function() {
    if (this.checked) optGroup.addTo(map);
    else map.removeLayer(optGroup);
    updateDay(currentDay);
  });
  ['togIntersections','togCamping','togTrailheads','togResupply'].forEach(id =>
    document.getElementById(id).addEventListener('change', updateNodeVisibility));

  // ── Opacity sliders ──────────────────────────────────────────────────────
  document.getElementById('mapwarpOpacity').addEventListener('input', function() {
    mapwarperLayer.setOpacity(+this.value / 100);
    document.getElementById('opacityVal').textContent = this.value + '%';
  });
  document.getElementById('hillshadeOpacity').addEventListener('input', function() {
    hillshadeLayer.setOpacity(+this.value / 100);
    document.getElementById('hillshadeOpacityVal').textContent = this.value + '%';
  });
  document.getElementById('basemapOpacity').addEventListener('input', function() {
    activeBasemap.setOpacity(+this.value / 100);
    document.getElementById('basemapOpacityVal').textContent = this.value + '%';
  });
  function setBasemap(name) {
    const opacity = +document.getElementById('basemapOpacity').value / 100;
    map.removeLayer(activeBasemap);
    activeBasemap = BASEMAPS[name];
    activeBasemap.addTo(map); activeBasemap.setOpacity(opacity); activeBasemap.bringToBack();
    document.getElementById('basemapSel').value = name;
  }
  document.getElementById('basemapSel').addEventListener('change', function() {
    setBasemap(this.value);
  });

  // ── Dark mode ────────────────────────────────────────────────────────────
  function applyTheme(dark) {
    document.body.classList.toggle('dark', dark);
    trailTheme = dark ? TRAIL_THEMES.dark : TRAIL_THEMES.light;
    BG_NORMAL.color = trailTheme.bg;
    bgGroup.eachLayer(l => {
      if (l.setStyle && l !== selectedBgPoly) l.setStyle({ color: trailTheme.bg });
    });
    covGroup.eachLayer(l => { if (l.setStyle) l.setStyle({ color: trailTheme.cov }); });
    document.getElementById('legBg').style.background  = trailTheme.bg;
    document.getElementById('legCov').style.background = trailTheme.cov;
    document.getElementById('btnDark').textContent = dark ? '☀️' : '🌙';
    // Swap default light basemaps for the dark one (and back), but leave
    // deliberate picks like aerial/topo alone.
    const cur = document.getElementById('basemapSel').value;
    if (dark && (cur === 'OSM Grayscale' || cur === 'CartoDB Light')) setBasemap('CartoDB Dark');
    else if (!dark && cur === 'CartoDB Dark') setBasemap('OSM Grayscale');
    localStorage.setItem('smokiesTheme', dark ? 'dark' : 'light');
  }
  document.getElementById('btnDark').addEventListener('click', () =>
    applyTheme(!document.body.classList.contains('dark')));
  if (localStorage.getItem('smokiesTheme') === 'dark') applyTheme(true);

  // ── Left sidebar preset selectors ────────────────────────────────────────
  // Presets exist only at the published pace.  Loading one while the sliders
  // show something else would put an itinerary on screen that nobody asked
  // for, under a pace label that never produced it -- so when a custom pace is
  // set and a backend is available, a parameter change re-solves instead.
  document.querySelectorAll('input[name="max_day"], input[name="circuit"], input[name="resupply"], input[name="town"]')
    .forEach(el => el.addEventListener('change', () => {
      if (!paceIsDefault(paceFromUI()) && backendUrl()) solveCustom();
      else loadPreset(currentPresetFile());
    }));

  // ── Custom solve panel (only with a configured backend) ──────────────────
  const bkParam = new URLSearchParams(location.search).get('backend');
  if (bkParam === 'off')  localStorage.removeItem('smokiesBackend');
  else if (bkParam)       localStorage.setItem('smokiesBackend', bkParam);
  ['paceV0', 'paceK', 'pacePeak'].forEach(id =>
    document.getElementById(id).addEventListener('input', renderPace));
  document.querySelectorAll('#paceTiers button').forEach(b =>
    b.addEventListener('click', () => setPaceUI(
      { v0: +b.dataset.v0, k: +b.dataset.k, peak: +b.dataset.peak })));
  const qp = new URLSearchParams(location.search);
  if (qp.has('v0') || qp.has('k') || qp.has('peak')) {
    setPaceUI({ v0: +(qp.get('v0') ?? PACE_DEFAULT.v0),
                k:  +(qp.get('k')  ?? PACE_DEFAULT.k),
                peak: +(qp.get('peak') ?? PACE_DEFAULT.peak) });
  } else {
    renderPace();
  }

  if (backendUrl()) {
    document.getElementById('customSolve').style.display = 'block';
    document.getElementById('btnSolve').addEventListener('click', solveCustom);
  }

  // Load the default preset on startup
  loadPreset('preset_open_12h.json');
});
