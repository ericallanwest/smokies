'use strict';

// ── Constants ──────────────────────────────────────────────────────────────
const HOME_BOUNDS = [[35.24, -84.10], [35.89, -82.97]];
const NPS_BASE    = 'https://raw.githubusercontent.com/nationalparkservice/symbol-library/gh-pages/src/standalone/';
const LINES_URL   = 'https://raw.githubusercontent.com/ericallanwest/smokies/main/lines_20250211.geojson';
const POINTS_URL  = 'https://raw.githubusercontent.com/ericallanwest/smokies/main/points_20250211.geojson';
const ICON_MAP    = { BC:'campsite', SH:'shelter', CG:'trailer-site', TH:'trailhead', TI:'sign', RI:'sign' };

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

function buildGeomAndDays(itinerary, directedGeom, namedGeom, nodeCoords) {
  const geomDict = {}, geomDirCache = {};
  const globalSeen = new Set();
  let cumReqMiles = 0;
  const covByDay = [], daysData = [];

  function resolveCoords(u, v, trailName, edgeId, isDh) {
    const gkey = (!isDh && edgeId != null) ? String(edgeId) : undirectedKey(u, v);
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
    let dayTotalS = 0, dayReqS = 0, dayDhS = 0;
    let dayMiles = 0, dayGain = 0, dayReqMiles = 0, dayNewReqMiles = 0;

    for (const arc of dayInfo.arcs) {
      const { from: u, to: v, is_deadhead: isDh, edge_id: eid, trail, miles, seconds, gain } = arc;
      const { gkey, geomFwd } = resolveCoords(u, v, isDh ? null : trail, eid, isDh);
      dayTotalS += seconds; dayMiles += miles; dayGain += gain;
      const tlabel = isDh ? trail : trailLabel(trail);
      steps.push({
        key: gkey, eid, geom_fwd: geomFwd, trail: tlabel,
        from: u, to: v, miles, seconds, gain,
        popup: `<b>${tlabel}</b><br>${u} → ${v}<br>${miles.toFixed(2)} mi &nbsp; ${fmtHM(seconds)}<br>+${gain.toLocaleString()} ft`,
        is_dh: isDh,
      });
      if (isDh) {
        dayDhS += seconds;
      } else {
        dayReqS += seconds; dayReqMiles += miles;
        if (eid != null && !globalSeen.has(eid)) { globalSeen.add(eid); dayNewReqMiles += miles; }
      }
    }
    cumReqMiles += dayNewReqMiles;
    covByDay.push(new Set(globalSeen));
    daysData.push({
      day: dayInfo.day, start_node: dayInfo.start_node, end_node: dayInfo.end_node,
      total_s: dayTotalS, req_s: dayReqS, dh_s: dayDhS,
      miles:         Math.round(dayMiles    * 100) / 100,
      gain:          Math.round(dayGain),
      req_miles:     Math.round(dayReqMiles * 100) / 100,
      cum_req_miles: Math.round(cumReqMiles * 100) / 100,
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
const reqGroup          = L.layerGroup();
const dhGroup           = L.layerGroup();
const arrowGroup        = L.layerGroup();
const optGroup          = L.layerGroup();
const intersectionGroup = L.layerGroup();
const campingGroup      = L.layerGroup();
const trailheadGroup    = L.layerGroup();

const BG_NORMAL  = { color:'#999',     weight:5, opacity:0.75 };
const BG_SEL     = { color:'#FFD700',  weight:7, opacity:1    };
const OPT_NORMAL = { color:'#f08080',  weight:5, opacity:0.75 };
const OPT_SEL    = { color:'#FFD700',  weight:7, opacity:1    };

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
const showDH = () => document.getElementById('togDH').checked;
function nodeName(id) { return NODE_NAME[id] || id; }

function buildItinerary(d) {
  const day = DAYS[d - 1];
  document.getElementById('itinerary').innerHTML = day.steps.map((s, i) => {
    const dot   = s.is_dh ? '#c0392b' : '#f7882f';
    const extra = s.is_dh ? ' <span style="color:#c0392b;font-size:10px">(backtrack)</span>' : '';
    return `<div class="itin-step" data-step="${i+1}" style="padding:3px 4px 3px 6px;border-radius:3px;cursor:pointer;border-left:3px solid transparent">
      <span style="color:${dot};font-weight:700">${i+1}.</span>
      <b>${s.trail}</b>${extra}<br>
      <span style="color:#666677;padding-left:12px">
        ${nodeName(s.from)} → ${nodeName(s.to)}<br>
        ${s.miles.toFixed(2)} mi &nbsp; ${fmtHM(s.seconds)} &nbsp; +${s.gain.toLocaleString()} ft
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

function renderForStep(d, step) {
  const day = DAYS[d - 1];
  [covGroup, reqGroup, dhGroup, arrowGroup].forEach(g => g.clearLayers());

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
    if (isLast)     { lineOpts = { color:'#FFD700', weight:7, opacity:1 };                         arrowColor = '#FFD700';  }
    else if (s.is_dh) {
      if (!showDH()) continue;
      lineOpts = { color:'#c0392b', weight:5, opacity:0.9, dashArray:'6,5' }; arrowColor = '#c0392b';
    } else          { lineOpts = { color:'#f7882f', weight:5, opacity:1 };                         arrowColor = '#f7882f';  }
    addPolyDecorated(s.is_dh ? dhGroup : reqGroup, arrowGroup, s, lineOpts, arrowColor);
  }
}

function updateDay(d) {
  currentDay = d;
  const day = DAYS[d - 1];
  const tot = META.total_required_miles;

  [covGroup, reqGroup, dhGroup, arrowGroup].forEach(g => g.clearLayers());
  if (startMarker) { startMarker.remove(); startMarker = null; }
  if (endMarker)   { endMarker.remove();   endMarker   = null; }
  if (selectedBgPoly) { selectedBgPoly.setStyle(BG_NORMAL); selectedBgPoly = null; }

  for (const seg of BG) {
    if (!cumCov[d - 1].has(seg.eid)) continue;
    addPoly(covGroup, GEOM[seg.key], { color:'#333333', weight:5, opacity:0.85 },
      `<b>${seg.trail}</b><br>${seg.from} ↔ ${seg.to}<br>${seg.miles.toFixed(2)} mi &nbsp; ${fmtHM(seg.seconds)}<br>+${seg.gain.toLocaleString()} ft`,
      seg.trail);
  }
  for (const a of day.steps.filter(s => !s.is_dh))
    addPolyDecorated(reqGroup, arrowGroup, a, { color:'#f7882f', weight:5, opacity:1 }, '#f7882f');
  if (showDH())
    for (const a of day.steps.filter(s => s.is_dh))
      addPolyDecorated(dhGroup, arrowGroup, a, { color:'#c0392b', weight:5, opacity:0.9, dashArray:'6,5' }, '#c0392b');

  if (day.start_coords)
    startMarker = L.marker(day.start_coords, { icon:triIcon('#27ae60', true), zIndexOffset:1000 })
      .bindPopup(`<b>Day ${d} Start</b><br>${day.start_node}`).addTo(map);
  if (day.end_coords)
    endMarker = L.marker(day.end_coords, { icon:triIcon('#c0392b', false), zIndexOffset:1001 })
      .bindPopup(`<b>Day ${d} Camp</b><br>${day.end_node}`).addTo(map);

  // Sidebar stats
  document.getElementById('sbDay').textContent   = `Day ${d} of ${META.n_days}`;
  document.getElementById('sbRoute').textContent = `${nodeName(day.start_node)} → ${nodeName(day.end_node)}`;
  document.getElementById('sbTime').textContent  = fmtHM(day.total_s);
  document.getElementById('sbReq').textContent   = fmtHM(day.req_s);
  document.getElementById('sbDH').textContent    = fmtHM(day.dh_s);
  document.getElementById('sbMiles').textContent = day.miles.toFixed(1) + ' mi';
  document.getElementById('sbGain').textContent  = '+' + day.gain.toLocaleString() + ' ft';
  document.getElementById('sbReqMi').textContent = day.req_miles.toFixed(1) + ' mi';
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
  [bgGroup, covGroup, reqGroup, dhGroup, arrowGroup, optGroup,
   intersectionGroup, campingGroup, trailheadGroup].forEach(g => g.clearLayers());
  if (startMarker) { startMarker.remove(); startMarker = null; }
  if (endMarker)   { endMarker.remove();   endMarker   = null; }

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

  // Optional (non-required) segments
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
let _linesGJ = null, _pointsGJ = null;

function showLoading(on) {
  document.getElementById('loading').classList.toggle('visible', on);
}

async function ensureBaseData() {
  if (_linesGJ) return;
  [_linesGJ, _pointsGJ] = await Promise.all([
    fetch(LINES_URL).then(r  => { if (!r.ok)  throw new Error('lines GeoJSON failed');  return r.json(); }),
    fetch(POINTS_URL).then(r => { if (!r.ok)  throw new Error('points GeoJSON failed'); return r.json(); }),
  ]);
}

async function loadPreset(filename) {
  showLoading(true);
  const errEl = document.getElementById('presetError');
  errEl.style.display = 'none';
  try {
    await ensureBaseData();
    const itinerary = await fetch(`data/${filename}`)
      .then(r => { if (!r.ok) throw new Error(`${filename} not found — has it been pre-computed?`); return r.json(); });

    const { geom, named }               = buildDirectedGeom(_linesGJ);
    const nodeCoords                     = buildNodeCoords(_pointsGJ);
    const allNodes                       = buildAllNodes(_pointsGJ);
    const { geomDict, daysData, covByDay } = buildGeomAndDays(itinerary, geom, named, nodeCoords);
    const bgLayer                        = buildBgLayer(itinerary);
    const optLayer                       = buildOptionalLayer(_linesGJ, bgLayer, geomDict);
    const meta = {
      circuit: itinerary.circuit,
      n_days:  itinerary.n_days,
      total_required_miles: itinerary.total_required_miles,
    };
    initViz(meta, geomDict, daysData, bgLayer, optLayer, allNodes, covByDay);
  } catch (err) {
    errEl.textContent    = err.message;
    errEl.style.display  = 'block';
  } finally {
    showLoading(false);
  }
}

function currentPresetFile() {
  const max = document.querySelector('input[name="max_day"]:checked')?.value  ?? '12';
  const min = document.querySelector('input[name="min_day"]:checked')?.value  ?? '10';
  const cir = document.querySelector('input[name="circuit"]:checked')?.value  ?? 'open';
  return `preset_${cir}_${max}h_${min}h.json`;
}

// ── Node layer visibility (zoom-dependent) ─────────────────────────────────
function updateNodeVisibility() {
  const zoom = map.getZoom(), zoomOk = zoom >= 10, zoomSm = zoom >= 8 && zoom < 10;
  document.body.classList.toggle('zoom-small-icons', zoomSm);
  [['togIntersections', intersectionGroup],
   ['togCamping',       campingGroup],
   ['togTrailheads',    trailheadGroup]].forEach(([id, grp]) => {
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

  // Persistent layer groups
  [bgGroup, covGroup, reqGroup, dhGroup, arrowGroup].forEach(g => g.addTo(map));

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
  ['togIntersections','togCamping','togTrailheads'].forEach(id =>
    document.getElementById(id).addEventListener('change', updateNodeVisibility));
  document.getElementById('togOpt').addEventListener('change', function() {
    if (this.checked) { optGroup.addTo(map); bgGroup.bringToFront(); }
    else map.removeLayer(optGroup);
  });

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
  document.querySelectorAll('input[name="max_day"], input[name="min_day"], input[name="circuit"]')
    .forEach(el => el.addEventListener('change', () => loadPreset(currentPresetFile())));

  // Load the default preset on startup
  loadPreset('preset_open_12h_10h.json');
});
