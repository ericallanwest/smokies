'use strict';

// ── Constants ──────────────────────────────────────────────────────────────
const HOME_BOUNDS = [[35.24, -84.10], [35.89, -82.97]];
const NPS_BASE    = 'https://raw.githubusercontent.com/nationalparkservice/symbol-library/gh-pages/src/standalone/';
const LINES_URL   = 'https://raw.githubusercontent.com/ericallanwest/smokies/main/lines_20250211.geojson';
const POINTS_URL  = 'https://raw.githubusercontent.com/ericallanwest/smokies/main/points_20250211.geojson';
const ICON_MAP    = { BC:'campsite', SH:'shelter', CG:'trailer-site', TH:'trailhead', TI:'sign', RI:'sign' };
const RESUPPLY_ICON_URL = 'https://raw.githubusercontent.com/nationalparkservice/symbol-library/master/src/shielded/store-white-22.svg';
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

const BG_NORMAL  = { color:'#999',     weight:5, opacity:0.75 };
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
    const extra = s.cat === 'repeat'    ? ' <span style="color:#c0392b;font-size:10px">(repeat)</span>'
                : s.cat === 'connector' ? ' <span style="color:#c0392b;font-size:10px">(connector)</span>'
                : '';
    return `<div class="itin-step" data-step="${i+1}" style="padding:3px 4px 3px 6px;border-radius:3px;cursor:pointer;border-left:3px solid transparent">
      <span style="color:${dot};font-weight:700">${i+1}.</span>
      <b>${s.trail}</b>${extra}<br>
      <span style="color:#666677;padding-left:12px">
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
    row.style.background = active ? '#fff9e6' : '';
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
    addPoly(covGroup, GEOM[seg.key], { color:'#333333', weight:5, opacity:0.85 },
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
    addPoly(covGroup, GEOM[seg.key], { color:'#333333', weight:5, opacity:0.85 },
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
  document.getElementById('sbDay').textContent   = `Day ${d} of ${META.n_days}`;
  document.getElementById('sbRoute').textContent = `${nodeName(day.start_node)} → ${nodeName(day.end_node)}`;
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

  updateDay(1);
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
    };
    initViz(meta, geomDict, daysData, bgLayer, optLayer, allNodes, covByDay);
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
  document.getElementById('basemapSel').addEventListener('change', function() {
    const opacity = +document.getElementById('basemapOpacity').value / 100;
    map.removeLayer(activeBasemap);
    activeBasemap = BASEMAPS[this.value];
    activeBasemap.addTo(map); activeBasemap.setOpacity(opacity); activeBasemap.bringToBack();
  });

  // ── Left sidebar preset selectors ────────────────────────────────────────
  document.querySelectorAll('input[name="max_day"], input[name="circuit"], input[name="resupply"], input[name="town"]')
    .forEach(el => el.addEventListener('change', () => loadPreset(currentPresetFile())));

  // Load the default preset on startup
  loadPreset('preset_open_12h.json');
});
