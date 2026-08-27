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
  if (day.end_coords) {
    // Under Supported the day ends at a road where the crew is waiting, not at
    // a camp -- calling it "Camp" would be telling the hiker to sleep there.
    const endLbl = META.hiking_style === 'supported' ? 'Pick-up' : 'Camp';
    endMarker = L.marker(day.end_coords, { icon:triIcon('#c0392b', false), zIndexOffset:1001 })
      .bindPopup(`<b>Day ${d} ${endLbl}</b><br>${day.end_node}`).addTo(map);
  }

  // Sidebar stats
  const rsStop = META.resupply_plan?.find(s => s.day === d);
  document.getElementById('sbDay').textContent   = `Day ${d} of ${META.n_days}`;
  // A shuttled day did not walk here from yesterday's finish, so name where
  // the crew picked up.  Without this the two nodes read as a gap in the walk.
  const from = shuttledFrom(d - 1);
  const overToday = (META.days_over_budget ?? []).find(x => x.day === d);
  document.getElementById('sbRoute').textContent =
    (from ? `(driven from ${nodeName(from)}) ` : '') +
    `${nodeName(day.start_node)} → ${nodeName(day.end_node)}` +
    (rsStop ? ` · resupply: ${rsStop.name}` : '')
    + (overToday
        ? ` · ${(overToday.over_by / 3600).toFixed(1)} h over your daily limit`
        : '');
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
// A supported day is driven to, so it need not resume where the last one
// stopped.  Self-supported itineraries chain by construction and return 0.
function countShuttles(days) {
  let n = 0;
  for (let i = 0; i < days.length - 1; i++) {
    if (days[i].end_node !== days[i + 1].start_node) n++;
  }
  return n;
}

// Where the crew picked the hiker up the evening before, if anywhere.
function shuttledFrom(i) {
  if (i <= 0 || i >= DAYS.length) return null;
  const prev = DAYS[i - 1], cur = DAYS[i];
  return prev.end_node === cur.start_node ? null : prev.end_node;
}

function initViz(meta, geomDict, daysData, bgLayer, optLayer, allNodes, cov) {
  const csvBtn = document.getElementById('btnCsv');
  if (csvBtn) csvBtn.disabled = false;   // nothing to export until now
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
  const shuttles = countShuttles(daysData);
  document.getElementById('presetInfo').innerHTML =
    `<div class="info-row"><span>Style</span>          <span><b>${
       meta.hiking_style === 'supported' ? 'Supported' : 'Self-supported'}</b></span></div>` +
    `<div class="info-row"><span>Route</span>          <span><b>${
       meta.circuit === 'Closed' ? 'Loop' : 'Point to point'}</b></span></div>` +
    `<div class="info-row"><span>Days</span>           <span><b>${meta.n_days}</b></span></div>` +
    (shuttles
      ? `<div class="info-row"><span>Crew shuttles</span> <span><b>${shuttles}</b></span></div>` : '') +
    ((meta.ferry_landings ?? []).length
      ? `<div class="info-row"><span>Ferry needed</span> <span><b>${
          meta.ferry_landings.map(f => f.name).join(', ')}</b></span></div>` : '') +
    ((meta.days_over_budget ?? []).length
      ? `<div class="info-row"><span>Days over budget</span> <span><b>${
          meta.days_over_budget.length} (to ${(Math.max(
            ...meta.days_over_budget.map(x => x.seconds)) / 3600).toFixed(1)} h)`
        + `</b></span></div>` : '') +
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
    // 'supported' means the crew repositions the hiker between days, so the
    // days need not chain -- which the map and the CSV both have to say.
    hiking_style: itinerary.hiking_style || 'self-supported',
    // Which ferry landings this itinerary depends on -- the solver records it,
    // because a hiker has to book them before committing to the trip.
    ferry_landings: itinerary.ferry_landings ?? [],
    // Days that run past the requested budget. Non-empty only where no
    // arrangement of days could stay inside it, so it is a fact about the park
    // rather than a slack itinerary — but the hiker has to be told.
    days_over_budget: itinerary.days_over_budget ?? [],
    n_days:  itinerary.n_days,
    // Where this itinerary begins.  Published presets are built from a swept
    // start rather than the default one -- worth 120 days across the 114 --
    // and the Start field is filled in from this so the hiker can see it, and
    // so re-solving at the same settings returns the published day count
    // rather than the default start's, which is usually worse.
    start_node: itinerary.start_node || null,
    total_required_miles: itinerary.total_required_miles,
    // Present only on resupply presets: minimal stop schedule computed by
    // the solver (hiker starts fully supplied, stops as late as the window
    // allows).  [{day, node, name, in_park, days_since_last}]
    resupply_plan: itinerary.resupply_plan || null,
    max_days_between_resupply: itinerary.max_days_between_resupply || null,
  };
  initViz(meta, geomDict, daysData, bgLayer, optLayer, allNodes, covByDay);
}

async function loadPreset(key) {
  const seq = ++_loadSeq;
  showLoading(true);
  const errEl = document.getElementById('presetError');
  errEl.style.display = 'none';
  try {
    await ensureBaseData();
    const index = await loadPresetIndex();
    const entry = index?.presets?.[key];
    // A gap in the grid is not an oversight, and the index says which kind it
    // is: a combination too tight to solve, or -- under Supported -- one the
    // park's geography rules out outright.  Either way the reason is worth
    // more than a broken link.
    if (!entry || entry.unavailable) {
      throw new Error((entry?.unavailable
        ?? 'No itinerary for this combination.')
        + (backendUrl() ? ' Or press Build itinerary to attempt it directly.' : ''));
    }
    const itinerary = await fetch(`data/${entry.file}`)
      .then(r => {
        if (!r.ok) throw new Error(`Could not load ${entry.file}.`);
        return r.json();
      });
    if (seq !== _loadSeq) return;   // superseded by a newer selection
    await renderItinerary(itinerary);
    // Presets are published at four paces now, so the itinerary on screen was
    // built at whichever one the buttons are on -- not always the default.
    _shownPace = { ...paceFromUI() };
    applyPresetStart();
    // Again now META is loaded: the style note reports this itinerary's
    // over-budget days, which are not known until it arrives.
    renderParamControls();
    renderPace();
  } catch (err) {
    if (seq !== _loadSeq) return;
    errEl.textContent    = err.message;
    errEl.style.display  = 'block';
  } finally {
    if (seq === _loadSeq) showLoading(false);
  }
}

// ── Configuration → published preset ──────────────────────────────────────
// The filename used to be rebuilt here from the control values, which made this
// a second copy of the batch tools' label scheme.  It now resolves through
// data/presets_index.json, so the generator decides what exists and this only
// asks.  That also means a configuration with no itinerary arrives as a
// sentence explaining why rather than a 404 -- which matters far more now that
// sliders put every combination one drag away.
let _index = null;

async function loadPresetIndex() {
  if (_index) return _index;
  _index = await fetch('data/presets_index.json').then(r => r.ok ? r.json() : null);
  return _index;
}

function styleFromUI() {
  return document.querySelector('input[name="style"]:checked')?.value ?? 'selfsup';
}

function maxDayFromUI() {
  return +(document.getElementById('maxDay')?.value ?? 12);
}

// The slider runs 4..8 and then one position past the end, which is "no limit".
function resupplyFromUI() {
  if (styleFromUI() === 'supported') return null;   // never applies
  const v = +(document.getElementById('resupply')?.value ?? 9);
  return v > 8 ? null : v;
}

// Keep the slider readouts, the resupply gate and the style note in step with
// the controls.  Called on every input event, so it must stay cheap and must
// not touch the map.
function renderParamControls() {
  renderFerryPicker();
  const style = styleFromUI();
  const hours = maxDayFromUI();
  const out   = document.getElementById('maxDayOut');
  if (out) out.textContent = `${hours} h`;

  const rsEl  = document.getElementById('resupply');
  const rsOut = document.getElementById('resupplyOut');
  const rsGrp = document.getElementById('resupplyGroup');
  const supported = style === 'supported';
  if (rsEl)  rsEl.disabled = supported;
  if (rsGrp) rsGrp.classList.toggle('disabled', supported);
  if (rsOut) {
    // A supported hiker meets the crew daily, so a resupply window is not a
    // constraint that can bind -- say that rather than showing a stale number.
    rsOut.textContent = supported ? 'n/a'
      : (+rsEl.value > 8 ? '∞' : rsEl.value);
  }

  const fpGrp = document.getElementById('ferryPresetGroup');
  if (fpGrp) fpGrp.style.display = supported ? '' : 'none';
  const fpNote = document.getElementById('ferryPresetNote');
  if (fpNote) {
    fpNote.textContent = ferryAllowedFromUI()
      ? 'The boat reaches Lakeshore, which is hours from any road at both '
        + 'ends. Allowing it is what makes the shorter days possible — but it '
        + 'has to be booked.'
      : 'Roads only. The remotest required trail then takes 13.7 h pick-up to '
        + 'pick-up, so nothing shorter than that exists without the boat.';
  }

  const note = document.getElementById('styleNote');
  if (!note) return;
  // What exists is the grid's answer, not a constant's: Heavy pack has no 8 h
  // supported itinerary where Standard does, and roads-only has none below
  // 11 h.  Ask the index about the combination actually selected.
  const gap = _index?.presets?.[currentPresetKey()]?.unavailable;
  if (gap) {
    // Not a missing preset: the park itself rules this out.  Say so here, at
    // the control, rather than waiting for the load to fail.
    note.textContent = gap;
    note.classList.add('warn');
  } else if (supported) {
    const ob = META?.days_over_budget ?? [];
    // The two things a hiker needs here are independent, so neither branch can
    // own the message: an itinerary can have days running over *and* sit below
    // what a live solve can manage, which is exactly the case at 8 h and 9 h.
    const below = hours < supportedFloor();
    let text;
    if (ob.length && META?.hiking_style === 'supported') {
      // Where days run over, say by how little. "4 days over budget" reads as a
      // broken itinerary; "4 days reach 9.2 h against your 8 h" reads as the
      // trade-off it actually is, and the hiker can judge it.
      const worst = Math.max(...ob.map(x => x.seconds)) / 3600;
      text = `${ob.length} of these days run past ${hours} h — the longest is `
        + `${worst.toFixed(1)} h. Nothing shorter is possible: the remotest `
        + `required trail cannot be crossed pick-up to pick-up in less.`;
    } else {
      text = 'A crew meets you each night, so every day starts and ends at a '
        + 'road — or at a Fontana Lake ferry landing where these itineraries '
        + 'need one. Expect more days than self-supported.';
    }
    if (below) {
      const blocked = hours * 1.5 < supportedFloor();
      text += blocked
        ? ` Build cannot run here: without a ferry landing no day reaches the `
          + `remotest trail and returns in under ${supportedFloor()} h.`
        : ` A custom Build at this length will return days that run over too.`;
    }
    note.textContent = text;
    note.classList.toggle('warn', hours * 1.5 < supportedFloor());
  } else {
    note.textContent = '';
    note.classList.remove('warn');
  }
}

// The custom-solve ferry picker.  Published presets take a landing only where
// roads alone give no itinerary; here the hiker decides for themselves, because
// whether a ferry is worth its cost and timetable is not ours to assume.
function renderFerryPicker() {
  const grp = document.getElementById('ferryGroup');
  if (!grp) return;
  // Gated by the sidebar's own ferry checkbox: a hiker who has said they will
  // not take the boat should not then be asked which boat.
  const supported = styleFromUI() === 'supported' && ferryAllowedFromUI();
  grp.style.display = supported ? 'block' : 'none';
  if (!supported) {
    document.querySelectorAll('input[name="ferry"]')
      .forEach(el => { el.checked = false; });
    return;
  }

  const box = document.getElementById('ferryOptions');
  if (box && !box.children.length) {
    for (const f of ferryLandings()) {
      const lab = document.createElement('label');
      // Ticked by default, so a Build starts from the same set the published
      // itinerary used.  It defaulted to none, which is why a 12 h supported
      // Build returned days nobody could walk while the 12 h preset was fine.
      lab.innerHTML = `<input type="checkbox" name="ferry" value="${f.node}" checked> ${f.name}`;
      box.appendChild(lab);
    }
    box.addEventListener('change', renderFerryPicker);
  } else if (box && !document.querySelector('input[name="ferry"]:checked')) {
    document.querySelectorAll('input[name="ferry"]')
      .forEach(el => { el.checked = true; });
  }
  const n = document.querySelectorAll('input[name="ferry"]:checked').length;
  const note = document.getElementById('ferryNote');
  if (note) {
    note.textContent = n
      ? `${n} landing${n > 1 ? 's' : ''} offered. Each is a boat you have to `
        + `book and pay for.`
      : 'Roads only. Below 14 h there is no supported itinerary without a '
        + 'ferry — tick one or more and build.';
  }
}

function ferryFromUI() {
  return [...document.querySelectorAll('input[name="ferry"]:checked')]
    .map(el => el.value);
}

// Whether a *published* itinerary may depend on the boat.  Distinct from
// ferryFromUI above, which picks individual landings for a custom solve: a
// preset either was built with the landings available or was not.
function ferryAllowedFromUI() {
  return document.getElementById('allowFerry')?.checked !== false;
}

// Which of the four published paces the sliders currently sit on, or null for
// anything in between.  Null is the signal that no preset can serve this and
// only a live solve can -- see the reselect handler.
function paceKeyFromUI() {
  const p = paceFromUI();
  const named = _index?.paces
    ?? [{ key: 'standard', v0: 6000, k: 3.5, peak: -0.05 }];
  return named.find(q => q.v0 === p.v0 && q.k === p.k && q.peak === p.peak)
    ?.key ?? null;
}

function currentPresetKey() {
  const rs = resupplyFromUI();
  const pace = paceKeyFromUI() ?? 'standard';
  if (styleFromUI() === 'supported') {
    return `supported_${maxDayFromUI()}h`
      + (ferryAllowedFromUI() ? '_ferry' : '_noferry') + `_${pace}`;
  }
  return `selfsup_${maxDayFromUI()}h` + (rs ? `_r${rs}` : '') + `_${pace}`;
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
    : paceKeyFromUI()
      ? 'Published pace — this itinerary is pre-solved at these settings.'
      : 'This itinerary was built at this pace.';

  // The four buttons in the sidebar select published itineraries; the sliders
  // below do not, and a hiker who has dragged one deserves to know that before
  // the preset list stops responding to them.
  const tierNote = document.getElementById('paceTierNote');
  if (tierNote) {
    tierNote.textContent = paceKeyFromUI()
      ? 'A slower pace is a different circuit, not the same one re-timed — '
        + 'each of these is solved separately.'
      : 'Custom pace: no published itinerary matches these sliders.'
        + (backendUrl() ? ' Build to solve one.' : '');
    tierNote.classList.toggle('warn', !paceKeyFromUI());
  }
  return p;
}

// Published presets are solved from a chosen start, not the default one, so
// the Start field is filled in from the itinerary on screen.  Left editable:
// clearing it or typing another trailhead is a normal thing to want, and the
// solver honours it either way.  Only ever overwrites a field the hiker has
// not touched -- an explicit choice outranks ours.
function applyPresetStart() {
  const el = document.getElementById('startNode');
  if (!el || !META || el.dataset.userEdited === '1') return;
  const node = META.start_node;
  el.value = node ? (_byId.get(node)?.name ?? node) : '';
  _shownEnds = { start: node || null, end: null };
  renderEndpoints();
}

// ── CSV download ──────────────────────────────────────────────────────────
// A 42-night trip is transcribed into the NPS permit system one campsite and
// one date at a time, then carried on paper or a phone.  Everything printed
// here is read off the same derived day objects the sidebar renders from, and
// through the same formatters, so the file cannot quietly disagree with the
// screen it came from.
function csvCell(value) {
  const text = value == null ? '' : String(value);
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

const csvRow = (...cells) => cells.map(csvCell).join(',');

// Dates make the file useful to the permit system, but demanding one before a
// hiker has even settled on a tier would be a toll gate on browsing.  Default
// to tomorrow -- the earliest plausible start -- and let anyone who is really
// planning override it.
function defaultStartDate() {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  return d.toISOString().slice(0, 10);
}

function dayDate(dayNumber) {
  const iso = document.getElementById('startDate')?.value || defaultStartDate();
  const [y, m, d] = iso.split('-').map(Number);
  // Built and read back in UTC.  A local-time Date would shift by a day across
  // a DST boundary, and a six-week trip crosses one.
  const t = new Date(Date.UTC(y, m - 1, d + (dayNumber - 1)));
  const wd = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][t.getUTCDay()];
  return wd + ' ' + t.toISOString().slice(0, 10);
}

// Fontana Lake ferry landings, from presets_index.json rather than a second
// hardcoded copy of the solver's list.
function ferryLandings() {
  return _index?.styles?.supported?.ferry_landings ?? [];
}
function isFerryNode(nid) {
  return ferryLandings().some(f => f.node === nid);
}

function overnightType(nid) {
  // Under Supported the hiker sleeps in town wherever the crew books a bed;
  // the node is only where they were collected, so naming it as lodging would
  // be wrong.
  if (META?.hiking_style === 'supported') {
    // Worth calling out separately: this one is a boat, and it has to be
    // booked.
    return isFerryNode(nid)
      ? 'ferry pick-up, Fontana Lake (bed in town)'
      : 'crew pick-up (bed in town)';
  }
  return nid.startsWith('BC') ? 'backcountry campsite'
       : nid.startsWith('SH') ? 'shelter'
       : nid.startsWith('CG') ? 'campground'
       : nid.startsWith('TH') ? 'trailhead (trip end)'
       : 'other';
}

function buildCsv() {
  const pace = paceFromUI();
  const walked = DAYS.reduce((a, d) => a + d.total_s, 0);
  const miles  = DAYS.reduce((a, d) => a + d.miles, 0);
  const gain   = DAYS.reduce((a, d) => a + d.gain, 0);
  const loss   = DAYS.reduce((a, d) => a + d.loss, 0);
  const maxDay = maxDayFromUI();
  const supported = META.hiking_style === 'supported';

  const meta = [
    ['Great Smokies Circuit Planner'],
    ['Hiking style', supported ? 'Supported (crew shuttle)' : 'Self-supported'],
    ['Route', META.circuit === 'Closed' ? 'Loop' : 'Point to point'],
    ['Days', META.n_days],
    ['Max hiking day', maxDay + ' h'],
    ['Required trail miles', META.total_required_miles.toFixed(1)],
    ['Distance walked', miles.toFixed(1) + ' mi'],
    ['Time walking', fmtHM(walked)],
    ['Elevation', gain.toLocaleString() + ' ft up / ' + loss.toLocaleString() + ' ft down'],
    ...((META.days_over_budget ?? []).length
      ? [['Days over budget',
          META.days_over_budget.length + ' - no arrangement of days fits this '
          + 'limit; see the daily summary'],
         ['Longest day',
          fmtHM(Math.max(...DAYS.map(d => d.total_s)))]] : []),
    ...(supported ? [['Ferry landings needed',
       (META.ferry_landings ?? []).map(f => f.name).join('; ')
       || 'none - every day reachable by road'],
      ['Ferry days',
       DAYS.filter(d => isFerryNode(d.start_node) || isFerryNode(d.end_node)).length
       + ((META.ferry_landings ?? []).length ? ' (Fontana Lake - book ahead)' : '')]] : []),
    ['Resupply window', supported ? 'n/a - the crew resupplies you'
      : (META.max_days_between_resupply
         ? 'every ' + META.max_days_between_resupply + ' days' : 'none')],
    ['Pace', (_index?.paces ?? []).find(q => q.key === paceKeyFromUI())?.label
      ?? (levelSpeedMph(pace).toFixed(1) + ' mph flat, k ' + pace.k.toFixed(1)
          + ', fastest ' + (pace.peak * 100).toFixed(0) + '%')],
  ];
  if (_shownEnds.start) meta.push(['Start pinned', nodeName(_shownEnds.start)]);
  if (_shownEnds.end)   meta.push(['Finish pinned', nodeName(_shownEnds.end)]);
  meta.push(['Day 1', dayDate(1)]);
  meta.push(['Generated', new Date().toISOString()]);
  meta.push(['Source', 'https://ericallanwest.github.io/smokies/']);

  // Nights first: this is the section that gets typed into a permit.  The last
  // day ends at a trailhead, not a campsite, so it is not a night.  Under
  // Supported there is no permit to file -- the night is a bed in town -- but
  // where the crew collects the hiker is exactly as worth writing down.
  const nights = [csvRow('day', 'date', 'night_at', 'name', 'type',
                         'next_day_starts_at')];
  DAYS.forEach((d, i) => {
    if (i === DAYS.length - 1) return;
    const moved = DAYS[i + 1].start_node !== d.end_node;
    nights.push(csvRow(d.day, dayDate(d.day), d.end_node,
                       nodeName(d.end_node), overnightType(d.end_node),
                       moved ? nodeName(DAYS[i + 1].start_node) : 'same place'));
  });

  const daily = [csvRow(
    'day', 'date', 'from', 'from_name', 'to', 'to_name', 'miles', 'time',
    'new_mi', 'repeat_mi', 'connector_mi', 'gain_ft', 'loss_ft',
    'cum_required_mi', 'cum_pct', 'shuttled_from')];
  for (const d of DAYS) {
    const from = shuttledFrom(DAYS.indexOf(d));
    daily.push(csvRow(
      d.day, dayDate(d.day), d.start_node, nodeName(d.start_node),
      d.end_node, nodeName(d.end_node),
      d.miles.toFixed(2), fmtHM(d.total_s),
      d.req_miles.toFixed(2), d.rep_miles.toFixed(2), d.conn_miles.toFixed(2),
      d.gain, d.loss, d.cum_req_miles.toFixed(1),
      (d.cum_req_miles / META.total_required_miles * 100).toFixed(1),
      from ? nodeName(from) : ''));
  }

  // Leg detail.  'type' is the walk-order category the map colours by -- first
  // pass, repeat, or connector -- rather than the solver's Euler bookkeeping,
  // because that is what the hiker actually experiences.
  const legs = [csvRow(
    'day', 'leg', 'trail', 'from', 'from_name', 'to', 'to_name', 'type',
    'miles', 'minutes', 'gain_ft', 'loss_ft', 'cum_day_mi', 'cum_day_time')];
  for (const d of DAYS) {
    let cm = 0, cs = 0;
    d.steps.forEach((s, i) => {
      cm += s.miles; cs += s.seconds;
      legs.push(csvRow(
        d.day, i + 1, s.trail, s.from, nodeName(s.from), s.to, nodeName(s.to),
        s.cat, s.miles.toFixed(2), (s.seconds / 60).toFixed(1),
        s.gain, s.loss, cm.toFixed(2), fmtHM(cs)));
    });
  }

  const out = [
    ...meta.map(c => csvRow(...c)),
    '', 'NIGHTS - one row per permit night', ...nights,
    '', 'DAILY SUMMARY', ...daily,
    '', 'LEG DETAIL', ...legs,
  ];

  if (META.resupply_plan && META.resupply_plan.length) {
    out.push('', 'RESUPPLY - ' + META.resupply_plan.length + ' stops',
      csvRow('day', 'date', 'place', 'in_park', 'days_since_last'));
    for (const r of META.resupply_plan) {
      out.push(csvRow(r.day, dayDate(r.day), r.name,
                      r.in_park ? 'yes' : 'no - town miles not counted',
                      r.days_since_last));
    }
  }
  return out.join('\r\n');
}

function downloadCsv() {
  if (!DAYS || !DAYS.length) return;
  // The BOM is load-bearing: without it Excel on Windows reads the file in the
  // local codepage and turns every trail name carrying an apostrophe or accent
  // into mojibake.
  const blob = new Blob(['﻿' + buildCsv()], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  const bits = [META.hiking_style === 'supported' ? 'supported' : 'selfsup',
                maxDayFromUI() + 'h'];
  if (META.max_days_between_resupply) bits.push('r' + META.max_days_between_resupply);
  const pk = paceKeyFromUI();
  // The pace is part of what the itinerary *is* now, so it belongs in the
  // filename even at Standard -- two downloads that differ only by pace would
  // otherwise land on top of each other.
  if (pk) bits.push(pk);
  else bits.push('k' + paceFromUI().k.toFixed(1));
  if (_shownEnds.start) {
    bits.push(_shownEnds.start + (_shownEnds.end ? '-' + _shownEnds.end : ''));
  }
  link.download = 'smokies_' + bits.join('_') + '.csv';
  link.click();
  // Revoked next tick rather than immediately: the click only starts the save,
  // and a browser reading the blob asynchronously would hand back an empty file.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

// ── Start / finish ────────────────────────────────────────────────────────
// 107 trailheads and frontcountry campgrounds.  A native <datalist> gives
// substring type-ahead ("greenbrier" finds Porters Creek) without shipping a
// combobox library; names are unique, so a name round-trips to exactly one id.
let _startPoints = [];          // [{id, name, type, lat, lon}]
const _byName = new Map();
const _byId   = new Map();
let _shownEnds = { start: null, end: null };

async function loadStartPoints() {
  try {
    const r = await fetch('data/start_points.json');
    if (!r.ok) return;
    _startPoints = await r.json();
  } catch { return; }
  const dl = document.getElementById('startPointList');
  if (!dl) return;
  dl.innerHTML = '';
  for (const p of _startPoints) {
    _byName.set(p.name.toLowerCase(), p);
    _byId.set(p.id, p);
    const o = document.createElement('option');
    o.value = p.name;
    dl.appendChild(o);
  }
}

// Blank means "solver chooses", which is a real answer and usually the best
// one -- it is free to pick the pair that saves the most walking.
function resolveEndpoint(el) {
  const raw = el.value.trim();
  if (!raw) { el.classList.remove('bad'); return { ok: true, id: null }; }
  const hit = _byName.get(raw.toLowerCase())
           || _byId.get(raw.toUpperCase());
  el.classList.toggle('bad', !hit);
  return hit ? { ok: true, id: hit.id } : { ok: false, id: null, raw };
}

function endpointsFromUI() {
  const s = resolveEndpoint(document.getElementById('startNode'));
  const e = resolveEndpoint(document.getElementById('endNode'));
  return { start: s, end: e };
}

function renderEndpoints() {
  const note = document.getElementById('endpointNote');
  if (!note) return {};
  const { start, end } = endpointsFromUI();
  let msg, bad = false;
  if (!start.ok || !end.ok) {
    msg = `No trailhead or campground called "${(!start.ok ? start.raw : end.raw)}".`;
    bad = true;
  } else if (end.id && start.id && end.id === start.id) {
    // This used to be an error, back when a separate radio chose the circuit
    // type.  Naming one place twice is now how you ask for a loop.
    msg = 'Start and finish are the same place, so this builds a loop back to '
        + `${_byId.get(start.id)?.name ?? start.id}.`;
    bad = start.id !== _shownEnds.start || _shownEnds.end !== start.id;
  } else if (end.id && !start.id) {
    msg = 'A finish needs a start. Pick where you begin, or clear the finish.';
    bad = true;
  } else if (start.id || end.id) {
    const stale = start.id !== _shownEnds.start || end.id !== _shownEnds.end;
    msg = stale
      ? 'Pinned endpoints mean a different route, not the same one re-cut. '
        + 'Build to re-solve (about 10–25 s).'
      : 'This itinerary was built between these points.';
    bad = stale;
  } else {
    msg = 'Leave both blank and the solver picks the pair that saves the most '
        + 'walking. Name the same place twice for a loop.';
  }
  note.classList.toggle('dirty', bad);
  note.textContent = msg;
  return { start, end };
}

// A pace belongs in the URL: it is reproducible, so a link to one is a link to
// exactly one itinerary.
function paceToQuery(p) {
  return paceIsDefault(p) ? '' :
    `&v0=${p.v0}&k=${p.k}&peak=${p.peak}`;
}

// The deployed solve service.  ?backend=<url> still overrides it (and
// ?backend=off disables the panel entirely), so a local backend can be pointed
// at without touching this, but visitors get the pace controls by default.
const DEFAULT_BACKEND = 'https://smokies-solver-165021828782.us-east1.run.app';

function backendUrl() {
  const stored = localStorage.getItem('smokiesBackend');
  return stored === 'off' ? null : (stored || DEFAULT_BACKEND);
}

function setSolveProgress(pct, label) {
  const box = document.getElementById('solveProg');
  box.style.display = pct === null ? 'none' : 'block';
  if (pct !== null) {
    document.getElementById('solveProgFill').style.width = pct + '%';
    document.getElementById('solveProgLabel').textContent = label || '';
  }
}

// An open walk skips the journey home, so it saves the cheapest way back from
// finish to start.  That is the number a hiker weighs when picking endpoints:
// distant pairs save hours, adjacent ones save minutes.
function reportEndpointSaving(data) {
  const note = document.getElementById('endpointNote');
  const secs = data?.params?.saved_vs_loop_seconds;
  if (!note || !data?.params?.endpoints_pinned || !secs) return;
  const h = secs / 3600;
  note.classList.remove('dirty');
  note.textContent = `Built ${_byId.get(_shownEnds.start)?.name ?? _shownEnds.start}`
    + ` → ${_byId.get(_shownEnds.end)?.name ?? _shownEnds.end}, saving `
    + `${h.toFixed(1)} h against returning to the start.`;
}

// What a live solve can actually manage for a supported trip.  Every day has to
// begin and end where the crew can reach, so the remotest required trail sets a
// floor: 13.7 h on roads alone, 9.2 h once ferry landings are in play.  The
// published itineraries go below that only because they declare the days that
// run over instead of stretching one to absurdity, which the solver will not do
// -- ask it for a 8 h supported trip and it returns a thirty-six hour day.
function supportedFloor() {
  const f = _index?.styles?.supported?.solver_floor_hours;
  if (!f) return 0;
  // The sidebar checkbox is what decides this, not the landing picker below
  // it: the picker only exists with a backend configured, and the floor is a
  // fact about the itinerary either way.
  return ferryAllowedFromUI() ? f.ferry : f.roads;
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
    const body = {
      max_hours: maxDayFromUI(),
      max_resupply_days: resupplyFromUI(),
      style: styleFromUI() === 'supported' ? 'supported' : 'self-supported',
      shuttle_nodes: styleFromUI() === 'supported' ? ferryFromUI() : [],
      hiked,
      time_budget: 45,
    };
    // Refuse only where the best conceivable day is past the validator's own
    // "not a day anyone can walk" line, a half again over budget.  Between the
    // floor and that line a solve returns days that run over and say so, which
    // is a trade-off worth offering rather than a failure worth blocking.
    if (body.style === 'supported'
        && maxDayFromUI() * 1.5 < supportedFloor()) {
      const pre = _index?.presets?.[currentPresetKey()];
      throw new Error(
        `No supported day can reach the remotest required trail and return in `
        + `under ${supportedFloor()} h`
        + (body.shuttle_nodes.length ? '' : ' without a ferry landing')
        + `, so a ${maxDayFromUI()} h solve would stretch one day past anything `
        + `walkable.`
        + (body.shuttle_nodes.length ? '' : ' Pick a ferry landing to lower that.')
        + (pre?.days
           ? ` The published itinerary handles it instead: ${pre.days} days`
             + (pre.days_over_budget
                ? `, ${pre.days_over_budget} of which run over and say so.` : '.')
           : ''));
    }
    const ends = endpointsFromUI();
    if (!ends.start.ok || !ends.end.ok) {
      throw new Error('That start or finish is not a trailhead or campground.');
    }
    if (ends.end.id && !ends.start.id) {
      throw new Error('Pick a start as well as a finish, or clear the finish.');
    }
    if (ends.start.id) body.start_node = ends.start.id;
    if (ends.end.id)   body.end_node   = ends.end.id;
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
    // The solver answers a same-start-and-finish request in "closed", and says
    // so in params, since that request is a loop rather than a path.
    const cir = result.params?.closed_from_equal_endpoints ? 'closed' : 'open';
    const itinerary = result[cir] || result.open || result.closed;
    if (!itinerary) throw new Error('solver found no valid itinerary for these settings');
    await renderItinerary(itinerary);
    _shownPace = pace;
    _shownEnds = { start: ends.start.id, end: ends.end.id };
    renderEndpoints();
    reportEndpointSaving(result);
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
  const reselect = () => {
    renderParamControls();
    if (!paceKeyFromUI() && backendUrl()) solveCustom();
    else loadPreset(currentPresetKey());
  };
  document.querySelectorAll('input[name="style"]')
    .forEach(el => el.addEventListener('change', reselect));
  document.getElementById('allowFerry')?.addEventListener('change', reselect);
  // 'input' rather than 'change' would re-solve on every pixel of a drag; the
  // readouts update live, the itinerary follows when the slider is let go.
  ['maxDay', 'resupply'].forEach(id => {
    const el = document.getElementById(id);
    el.addEventListener('input', renderParamControls);
    el.addEventListener('change', reselect);
  });

  // ── Custom solve panel (only with a configured backend) ──────────────────
  const bkParam = new URLSearchParams(location.search).get('backend');
  if (bkParam === 'off')  localStorage.setItem('smokiesBackend', 'off');
  else if (bkParam)       localStorage.setItem('smokiesBackend', bkParam);
  ['paceV0', 'paceK', 'pacePeak'].forEach(id => {
    const el = document.getElementById(id);
    el.addEventListener('input', renderPace);
    // Dragging onto one of the four published settings should load that
    // itinerary, the same as pressing its button would.
    el.addEventListener('change', () => { if (paceKeyFromUI()) reselect(); });
  });
  document.querySelectorAll('#paceTiers button').forEach(b =>
    b.addEventListener('click', () => {
      setPaceUI({ v0: +b.dataset.v0, k: +b.dataset.k, peak: +b.dataset.peak });
      reselect();     // a tier is one of the four published paces, so this
                      // swaps the itinerary rather than only the labels
    }));
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
    loadStartPoints().then(renderEndpoints);
    const dateEl = document.getElementById('startDate');
    if (dateEl && !dateEl.value) dateEl.value = defaultStartDate();
    document.getElementById('btnCsv')?.addEventListener('click', downloadCsv);
    for (const id of ['startNode', 'endNode']) {
      const el = document.getElementById(id);
      if (el) ['input', 'change'].forEach(ev => el.addEventListener(ev, () => {
        el.dataset.userEdited = '1';
        renderEndpoints();
      }));
    }
  }

  // Load the default preset on startup.  renderParamControls runs twice on
  // purpose: once now so the readouts are never blank, and again once the
  // index has landed, since it is the index that says which paces exist and
  // which combinations do not.
  //
  // The load itself waits for the index rather than racing it: currentPresetKey
  // has to name a pace, and before the index arrives every pace looks custom,
  // so a ?v0=5400 link would open the Standard itinerary and then sit there
  // claiming to be Heavy pack.
  renderParamControls();
  loadPresetIndex().then(() => {
    renderParamControls();
    renderPace();
    loadPreset(currentPresetKey());
  });
});
