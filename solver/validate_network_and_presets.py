#!/usr/bin/env python
"""Standalone audit of the trail network and every published preset itinerary.

Two independent questions, answered without importing the solver:

  1. Is the NETWORK topologically honest?  Every edge's declared end nodes must
     sit where the line geometry actually ends, the CSV edge list and the map
     geojson must agree, the graph must be one connected piece, and no node may
     sit on another trail's interior without a junction.  A missing junction is
     how the Chestnut Top / Little Greenbrier wormhole hid: node TH072 stood in
     for two trailheads 22,000 ft apart, so the solver believed Chestnut Top ran
     into Little Greenbrier at Wear Cove Gap instead of ending at the Townsend
     Wye.  The real node for that terminus, TH057, sat unused in the points file.

  2. Can the ITINERARIES be trusted?  Each preset is replayed arc by arc against
     the edge list: arcs must exist and connect, days must chain, every required
     trail must be covered exactly once, nights must fall at legal overnights
     that respect the consecutive-stay caps, no day may exceed its hour budget,
     resupply windows must hold, and no day may contain a closed all-deadhead
     loop -- repeat walking that covers nothing, the "hike down Spruce Mountain
     and then straight back up it" class of bug.

Usage:  python validate_network_and_presets.py [--data docs/data] [-v]
Exit code 0 = clean, 1 = at least one failure.
"""
import argparse, csv, glob, json, math, os, re, sys
from collections import Counter, defaultdict

AP = argparse.ArgumentParser()
AP.add_argument('--csv',  default='smokies_edge_list_20260509a.csv')
AP.add_argument('--data', default=os.path.join('docs', 'data'))
AP.add_argument('--endpoint-tolerance-ft', type=float, default=250.0)
AP.add_argument('-v', '--verbose', action='store_true')
A = AP.parse_args()

FAIL, WARN = [], []
def fail(cat, msg): FAIL.append((cat, msg))
def warn(cat, msg): WARN.append((cat, msg))

# Mirrors RESUPPLY_NODES in the solver and viz.js -- keep in sync.
RESUPPLY = {'CGCAD', 'TH264', 'TH210', 'TH158', 'TH025',
            'RI058', 'TH117', 'TH119', 'TH220', 'CGSMO'}
# Fontana Lake ferry landings a supported hiker can be collected from.  Mirrors
# FERRY_LANDINGS in the solver -- these are what let supported itineraries exist
# below 13.7 h at all, and they are opt-in because a ferry costs money and runs
# to a timetable.
SHUTTLE_NODES = {'TI051', 'TI053', 'TI064', 'BC090'}
SINGLE_NIGHT_PREFIX = ('SH',)          # shelters: 1 consecutive night
SINGLE_NIGHT_IDS    = {'BC113'}        # former shelter, same cap
BC_CONSEC_CAP       = 3                # backcountry sites: 3 consecutive nights

# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------
rows = list(csv.DictReader(open(A.csv, encoding='utf-8-sig')))
pts  = json.load(open(os.path.join(A.data, 'points_20250211.geojson')))
lns  = json.load(open(os.path.join(A.data, 'lines_20250211.geojson')))
NODE = {f['properties']['id']: f['properties'] for f in pts['features']}

# Edge ids are canonicalised to the "81.0" form the preset JSONs use.
edges = {str(float(r['ID'])): r for r in rows}

def legal_overnight(n, town):
    return n.startswith(('BC', 'SH', 'CG')) or (town and n in RESUPPLY)

def closed_trail(r):
    return str(r.get('is_trail_closed', '0')) == '1'

# ---------------------------------------------------------------------------
# 1. Network
# ---------------------------------------------------------------------------
def hav_ft(a, b):
    R = 20902231.0
    la1, lo1 = map(math.radians, a)
    la2, lo2 = map(math.radians, b)
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))

def verts(geom):
    if geom['type'] == 'LineString':
        return geom['coordinates']
    out = []
    for part in geom['coordinates']:
        out.extend(part)
    return out

geo = {}
for f in lns['features']:
    p = f['properties']
    geo[str(float(p['Segment']))] = (p, [(y, x) for x, y in verts(f['geometry'])])

print("=" * 68)
print("NETWORK")
print("=" * 68)

# 1a. every edge's nodes exist and sit at the line's real endpoints
worst = []
for sid, (p, cs) in geo.items():
    s, e = p['Start'], p['End']
    for n in (s, e):
        if n not in NODE:
            fail('network', "segment %s (%s): node %s not in points file"
                            % (sid, p['Name'], n))
    if s in NODE and e in NODE:
        ns = (NODE[s]['latitude'], NODE[s]['longitude'])
        ne = (NODE[e]['latitude'], NODE[e]['longitude'])
        a, b = cs[0], cs[-1]
        d = min(max(hav_ft(a, ns), hav_ft(b, ne)),
                max(hav_ft(a, ne), hav_ft(b, ns)))
        worst.append((d, sid, p['Name'], s, e))
        if d > A.endpoint_tolerance_ft:
            fail('network', "segment %s (%s) [%s-%s]: geometry ends %s ft from its "
                            "declared node(s)"
                            % (sid, p['Name'], s, e, format(d, ',.0f')))
worst.sort(reverse=True)
print("  edge endpoints vs node coordinates : %d checked, worst offset %s ft "
      "(tolerance %s ft)"
      % (len(worst), format(worst[0][0], ',.0f'),
         format(A.endpoint_tolerance_ft, ',.0f')))

# 1b. CSV edge list and map geojson describe the same network
n_mismatch = 0
for sid, r in edges.items():
    if sid not in geo:
        fail('network', "edge %s (%s) has no map geometry" % (sid, r['Trail']))
        n_mismatch += 1
        continue
    p, _ = geo[sid]
    if {p['Start'], p['End']} != {r['node_A'], r['node_B']}:
        fail('network', "edge %s (%s): CSV says %s-%s, map says %s-%s"
                        % (sid, r['Trail'], r['node_A'], r['node_B'],
                           p['Start'], p['End']))
        n_mismatch += 1
    elif abs(float(p['Miles']) - float(r['Miles'])) > 0.051:
        fail('network', "edge %s (%s): CSV %s mi vs map %s mi"
                        % (sid, r['Trail'], r['Miles'], p['Miles']))
        n_mismatch += 1
for sid in geo:
    if sid not in edges:
        fail('network', "map segment %s has no row in the edge list" % sid)
        n_mismatch += 1
print("  edge list vs map geometry          : %d edges, %d mismatch(es)"
      % (len(edges), n_mismatch))

# 1c. connectivity, orphans
adj, deg = defaultdict(set), Counter()
for r in rows:
    if closed_trail(r):
        continue
    a, b = r['node_A'], r['node_B']
    adj[a].add(b); adj[b].add(a); deg[a] += 1; deg[b] += 1
seen, comps = set(), []
for n in deg:
    if n in seen:
        continue
    stack, comp = [n], set()
    while stack:
        x = stack.pop()
        if x in comp:
            continue
        comp.add(x)
        stack.extend(adj[x] - comp)
    seen |= comp
    comps.append(comp)
comps.sort(key=len, reverse=True)
for c in comps[1:]:
    fail('network', "disconnected component of %d node(s): %s"
                    % (len(c), sorted(c)[:8]))
orphans = [n for n in NODE if n not in deg]
for n in orphans:
    fail('network', "node %s (%s) is in the points file but on no edge"
                    % (n, NODE[n]['name']))
for n in [n for n in deg if n not in NODE]:
    fail('network', "node %s is used by an edge but missing from the points file" % n)
print("  connectivity                       : %d nodes, %d component(s), "
      "%d orphan node(s)" % (len(deg), len(comps), len(orphans)))

# 1d. a node sitting on another trail's interior means a junction was not modelled
near = []
for nid, np_ in NODE.items():
    P = (np_['latitude'], np_['longitude'])
    for sid, (p, cs) in geo.items():
        if nid in (p['Start'], p['End']) or len(cs) <= 5:
            continue
        d = min(hav_ft(P, v) for v in cs[2:-2])
        if d < 40:
            fail('network', "node %s (%s) sits %.0f ft inside segment %s (%s) but "
                            "is not one of its nodes"
                            % (nid, np_['name'], d, sid, p['Name']))
        elif d < 120:
            near.append((d, nid, np_['name'], sid, p['Name']))
print("  unmodelled junctions               : 0 hard, %d within 120 ft "
      "(parallel trails -- review, not necessarily wrong)" % len(near))
if A.verbose:
    for d, nid, nn, sid, sn in sorted(near):
        print("      %5.0f ft  %s (%s) vs segment %s (%s)" % (d, nid, nn, sid, sn))

# 1e. geometry vertex order must run Start -> End.  Gain/Loss and the two
#     Tobler costs were integrated along the stored vertex order, so a segment
#     digitised backwards silently attaches them to the wrong direction -- which
#     is how Newfound Gap Road ended up costing less to climb than to descend.
backwards = []
for sid, (p, cs_) in geo.items():
    s, e = p['Start'], p['End']
    if s not in NODE or e not in NODE:
        continue
    ns = (NODE[s]['latitude'], NODE[s]['longitude'])
    ne = (NODE[e]['latitude'], NODE[e]['longitude'])
    a, b = cs_[0], cs_[-1]
    if max(hav_ft(a, ne), hav_ft(b, ns)) < max(hav_ft(a, ns), hav_ft(b, ne)):
        backwards.append(sid)
        fail('network', "segment %s (%s) [%s-%s]: geometry is digitised End->Start, "
                        "so its gain/loss and direction costs may be reversed"
                        % (sid, p['Name'], s, e))
print("  geometry vertex order              : %d segments, %d digitised backwards"
      % (len(geo), len(backwards)))

# 1f. the two direction costs must agree with which way is uphill
wrong_way = []
for sid, r in edges.items():
    if closed_trail(r):
        continue
    net = int(r['elev_B']) - int(r['elev_A'])
    slower_ab = int(r['cost_A_to_B']) > int(r['cost_B_to_A'])
    gain_net = int(r['gain_A_to_B']) - int(r['gain_B_to_A'])
    # Only judge edges whose net rise dominates the rolling terrain along them:
    # on a long, nearly level segment the two directions accumulate almost the
    # same gain, so which one is cheaper is decided by metres, not by the ends.
    if abs(net) >= 100 and (net > 0) != slower_ab:
        wrong_way.append(sid)
        fail('network', "edge %s (%s) climbs %+d ft from %s to %s but costs %ds that "
                        "way and %ds back -- the uphill direction is the cheaper one"
                        % (sid, r['Trail'], net, r['node_A'], r['node_B'],
                           int(r['cost_A_to_B']), int(r['cost_B_to_A'])))
    if abs(net) >= 25 and abs(-gain_net - net) < abs(gain_net - net) - max(6, .06*abs(net)):
        fail('network', "edge %s (%s): net rise is %+d ft but gain_A_to_B - gain_B_to_A "
                        "is %+d -- the gain pair looks reversed"
                        % (sid, r['Trail'], net, gain_net))
print("  cost direction vs elevation        : %d edges, %d with the uphill direction "
      "cheaper" % (len(edges), len(wrong_way)))

# ---------------------------------------------------------------------------
# 2. Presets
# ---------------------------------------------------------------------------
print()
print("=" * 68)
print("PRESET ITINERARIES")
print("=" * 68)

required_ids = {sid for sid, r in edges.items()
                if r['is_required'] == '1' and not closed_trail(r)}
required_miles = round(sum(float(edges[s]['Miles']) for s in required_ids), 1)

# Arc lookup keyed on (from, to, edge_id): every arc in a preset is checked
# against the real network rather than trusted from the JSON it came from.
arc_ok = {}
for sid, r in edges.items():
    if closed_trail(r):
        continue
    arc_ok[(r['node_A'], r['node_B'], sid)] = int(r['cost_A_to_B'])
    arc_ok[(r['node_B'], r['node_A'], sid)] = int(r['cost_B_to_A'])

# Costs are baked into the CSV at the standard pace, so a Heavy pack preset
# checked against them would fail every arc.  The histogram in
# segment_profiles.json re-times both directions of an edge from its 1% slope
# bins, and this recomputes that here rather than importing the code that built
# the presets -- a validator that shares an arithmetic bug with its subject
# proves nothing.  Mirrors PACES in tools/carp_common.py and the solver.
PACE_CONSTANTS = {
    'standard': (6000.0, 3.5, -0.05),
    'heavy':    (5400.0, 4.2, -0.05),
    'strong':   (6300.0, 3.2, -0.05),
    'fast':     (6600.0, 3.0, -0.04),
}
_PROFILES = None
_ARC_BY_PACE = {'standard': arc_ok}


def arcs_at_pace(pace):
    """(from, to, edge_id) -> seconds, for a pace other than the baked one."""
    global _PROFILES
    if pace in _ARC_BY_PACE:
        return _ARC_BY_PACE[pace]
    if _PROFILES is None:
        pf = os.path.join(A.data, 'segment_profiles.json')
        if not os.path.exists(pf):
            fail('data', 'segment_profiles.json is missing, so presets at a '
                         'pace other than standard cannot be checked at all')
            _PROFILES = {}
        else:
            _PROFILES = json.load(open(pf, encoding='utf-8'))['segments']
    v0, k, peak = PACE_CONSTANTS[pace]

    def secs(bins, reverse):
        t = 0.0
        for dist_m, rise_m in bins.values():
            if dist_m <= 0:
                continue
            s = rise_m / dist_m
            if reverse:
                s = -s
            t += dist_m / (v0 * math.exp(-k * abs(s - peak))) * 3600.0
        return t

    table = {}
    for sid, r in edges.items():
        if closed_trail(r):
            continue
        rec = _PROFILES.get(str(float(sid)))
        if rec is None:
            continue
        table[(r['node_A'], r['node_B'], sid)] = int(round(secs(rec['bins'], False)))
        table[(r['node_B'], r['node_A'], sid)] = int(round(secs(rec['bins'], True)))
    _ARC_BY_PACE[pace] = table
    return table


# preset_selfsup_12h_r6_standard.json / preset_supported_14h_noferry_fast.json.
# The old scheme carried a circuit word and a _town flag; both axes are gone --
# every preset is an open walk, and resupply points are always legal overnights.
# Hiking style took their place, then pace, and for supported trips whether the
# ferry may be used -- which had been a publishing decision rather than the
# hiker's.  Both new parts are optional, because the pre-pace filenames are kept
# as aliases until the frontend reads presets_index.json for its filename.
PAT = re.compile(r'^preset_(selfsup|supported)_(\d+)h(?:_r(\d+))?'
                 r'(?:_(ferry|noferry))?(?:_(heavy|standard|strong|fast))?'
                 r'\.json$')
files = sorted(glob.glob(os.path.join(A.data, 'preset_*.json')))
n_clean = 0
for fp in files:
    name = os.path.basename(fp)
    m = PAT.match(name)
    if not m:
        fail(name, "filename does not match the preset naming scheme")
        continue
    style, hours = m.group(1), int(m.group(2))
    rmax = int(m.group(3)) if m.group(3) else None
    # A filename with no ferry word predates the axis and states nothing about
    # the boat either way, so the no-ferry check below is skipped for it.
    ferry_ok = None if m.group(4) is None else (m.group(4) == 'ferry')
    pace = m.group(5) or 'standard'
    arc_ok = arcs_at_pace(pace)
    supported = style == 'supported'
    town = True          # resupply points are always legal overnights now
    if supported and rmax is not None:
        fail(name, "supported presets carry no resupply window")
    if not supported and ferry_ok:
        fail(name, "a self-supported preset cannot depend on the ferry")
    budget = hours * 3600
    d = json.load(open(fp))
    days = d['days']
    before = len(FAIL)

    # 2a. arcs are real, arcs connect, days chain
    prev_end = None
    for dd in days:
        arcs = dd['arcs']
        if not arcs:
            fail(name, "day %s has no arcs" % dd['day'])
            continue
        if arcs[0]['from'] != dd['start_node'] or arcs[-1]['to'] != dd['end_node']:
            fail(name, "day %s: start/end node disagrees with its arcs" % dd['day'])
        if prev_end is not None and arcs[0]['from'] != prev_end and not supported:
            # A supported day is driven to, so it need not resume where the
            # previous one stopped.  Check 2i below covers the rule that does
            # apply: both ends must be somewhere a vehicle can reach.
            fail(name, "day %s starts at %s but day %s ended at %s"
                       % (dd['day'], arcs[0]['from'], dd['day'] - 1, prev_end))
        prev_end = arcs[-1]['to']
        for k, a in enumerate(arcs):
            key = (a['from'], a['to'], a['edge_id'])
            if key not in arc_ok:
                fail(name, "day %s arc %d: %s->%s on edge %s (%s) is not in the "
                           "network" % (dd['day'], k, a['from'], a['to'],
                                        a['edge_id'], a['trail']))
            elif abs(arc_ok[key] - a['seconds']) > 1:
                fail(name, "day %s arc %d: edge %s costs %ds in the network, %ds here"
                           % (dd['day'], k, a['edge_id'], arc_ok[key], a['seconds']))
            if k and arcs[k - 1]['to'] != a['from']:
                fail(name, "day %s: arc %d starts at %s but arc %d ended at %s"
                           % (dd['day'], k, a['from'], k - 1, arcs[k - 1]['to']))

    # 2b. every required trail covered, exactly once as required walking
    covered = Counter(a['edge_id'] for dd in days for a in dd['arcs']
                      if not a['is_deadhead'])
    for sid in sorted(required_ids - set(covered)):
        fail(name, "required edge %s (%s, %s) is never covered"
                   % (sid, edges[sid]['Trail'], edges[sid]['Endpoints']))
    for sid, c in sorted(covered.items()):
        if sid not in required_ids:
            fail(name, "edge %s (%s) is marked covered but is not a required trail"
                       % (sid, edges[sid]['Trail']))
        elif c > 1:
            fail(name, "required edge %s (%s) counted as covered %d times"
                       % (sid, edges[sid]['Trail'], c))
    if abs(d.get('total_required_miles', 0) - required_miles) > 0.15:
        fail(name, "total_required_miles is %s, edge list says %s"
                   % (d.get('total_required_miles'), required_miles))

    # 2c. daily budget
    # A supported preset may declare days that run over: parts of the park
    # cannot be covered inside a short pick-up-to-pick-up day at any day count,
    # and naming the long days beats publishing nothing.  Every such day has to
    # be declared, though -- an undeclared one is a day the hiker is not warned
    # about.
    declared_over = {x['day'] for x in d.get('days_over_budget', [])}
    for dd in days:
        t = sum(a['seconds'] for a in dd['arcs'])
        if t > budget and dd['day'] not in declared_over:
            fail(name, "day %s takes %.1fh, over the %dh budget, and is not "
                       "declared in days_over_budget"
                       % (dd['day'], t / 3600.0, hours))
        elif t <= budget and dd['day'] in declared_over:
            fail(name, "day %s is declared over budget but takes only %.1fh"
                       % (dd['day'], t / 3600.0))
        elif t > budget * 1.5:
            fail(name, "day %s takes %.1fh, more than 1.5x the %dh budget -- "
                       "that is not a day anyone can walk"
                       % (dd['day'], t / 3600.0, hours))

    # 2d. overnights: legal, and within the consecutive-stay caps.
    #     Supported skips this entirely -- the hiker is driven to a bed, so the
    #     rule is check 2i's "a vehicle can reach both ends", and consecutive
    #     stays are a backcountry permit concern that no longer applies.
    last, consec = None, 0
    for dd in ([] if supported else days[:-1]):
        n = dd['end_node']
        if not legal_overnight(n, town):
            fail(name, "day %s ends the night at %s, which is not a legal overnight%s"
                       % (dd['day'], n, "" if town else " (town nights off)"))
        consec = consec + 1 if n == last else 1
        if n.startswith(SINGLE_NIGHT_PREFIX) or n in SINGLE_NIGHT_IDS:
            cap = 1
        elif town and n in RESUPPLY:
            cap = 99
        else:
            cap = BC_CONSEC_CAP
        if consec > cap:
            fail(name, "day %s: %d consecutive nights at %s (cap %d)"
                       % (dd['day'], consec, n, cap))
        last = n

    # 2e. resupply window (pass-through: any touch during the day counts; the
    #     hiker starts supplied and the final day is exempt)
    if rmax is not None:
        since = 0
        for dd in days[:-1]:
            since += 1
            if {a['to'] for a in dd['arcs']} & RESUPPLY:
                since = 0
            elif since > rmax:
                fail(name, "day %s: %d days without resupply (window is %d)"
                           % (dd['day'], since, rmax))
        if d.get('max_days_between_resupply') != rmax:
            fail(name, "max_days_between_resupply is %s, filename says %d"
                       % (d.get('max_days_between_resupply'), rmax))

    # 2f. no closed all-deadhead loop inside a day -- repeat walking that covers
    #     nothing.  A resupply out-and-back is a real trip, not a stranded loop,
    #     so one whose removal would cost the day its resupply touch is a note.
    for dd in days:
        arcs = dd['arcs']
        seq = [arcs[0]['from']] + [a['to'] for a in arcs]
        claimed = [False] * len(arcs)
        rs_before = {a['to'] for a in arcs} & RESUPPLY
        for i in range(len(arcs)):
            if claimed[i]:
                continue
            for j in range(len(arcs), i, -1):
                if seq[j] != seq[i] or any(claimed[k] for k in range(i, j)):
                    continue
                if not all(arcs[k]['is_deadhead'] for k in range(i, j)):
                    continue
                rs_after = {a['to'] for k, a in enumerate(arcs)
                            if not (i <= k < j)} & RESUPPLY
                mi = sum(arcs[k]['miles'] for k in range(i, j))
                for k in range(i, j):
                    claimed[k] = True
                if rs_before != rs_after:
                    warn(name, "day %s: %.1f mi out-and-back from %s kept -- it is "
                               "the day's only resupply touch" % (dd['day'], mi, seq[i]))
                else:
                    fail(name, "day %s: %.1f mi closed deadhead loop from %s covers "
                               "nothing" % (dd['day'], mi, seq[i]))
                break

    # 2g. an open walk must not begin or end on a deadhead arc.  Its first leg
    #     would cover nothing, and the hiker could simply have been dropped at
    #     the far end of it -- the default 12h itinerary used to open with 0.9
    #     mi of Heintooga Ridge Road for no reason at all.  Only a failure when
    #     the trim was actually available, i.e. it would leave the walk on a
    #     trailhead or campground: that is the same rule trim_open_termini
    #     applies in the solver, so this cannot demand something impossible.
    flat = [a for dd in days for a in dd['arcs']]
    for where, run_arcs, node_of in (
            ('begins', flat, lambda a: a['to']),
            ('ends', flat[::-1], lambda a: a['from'])):
        run = 0
        while run < len(run_arcs) and run_arcs[run]['is_deadhead']:
            run += 1
        if not run:
            continue
        a0 = run_arcs[0]
        trimmable = any(node_of(run_arcs[k])[:2] in ('TH', 'CG')
                        for k in range(run))
        where_msg = ("walk %s on %d deadhead arc(s), first %s->%s (%s), "
                     "covering nothing"
                     % (where, run, a0['from'], a0['to'], a0['trail']))
        if trimmable:
            fail(name, where_msg + " -- trimmable to a trailhead")
        else:
            warn(name, where_msg + " -- no trailhead inside the run, so it "
                                   "cannot be trimmed away")

    # 2i. supported: every day is collected from, so both of its ends have to
    #     be somewhere the crew can reach.  This is the rule that replaces 2a's
    #     day-chaining and 2d's overnight legality for this style, and it is the
    #     one that would catch a day-split trim cutting too deep.
    #
    #     Usually that means a road, but not always: TI051 is the Hazel Creek
    #     landing on Fontana Lake, reachable by the boat shuttle, and it is what
    #     lets supported itineraries exist below 13.7 h at all.  Mirrors
    #     SHUTTLE_NODES in the solver -- keep in sync.
    if supported:
        for dd in days:
            for role, n in (('starts', dd['start_node']), ('ends', dd['end_node'])):
                if n[:2] not in ('TH', 'CG') and n not in SHUTTLE_NODES:
                    fail(name, "day %s %s at %s, which no car or boat can "
                               "reach -- a supported hiker cannot be dropped "
                               "off or collected there" % (dd['day'], role, n))
        moved = sum(1 for k in range(len(days) - 1)
                    if days[k]['end_node'] != days[k + 1]['start_node'])
        if not moved:
            warn(name, "no day is repositioned by the crew, so this itinerary "
                       "gains nothing from being supported")

        # The declared ferry dependency has to match the itinerary: a hiker
        # books off this list, so a landing the walk uses but the preset does
        # not name is a trip that strands them at the water.
        used = {n for dd in days for n in (dd['start_node'], dd['end_node'])
                if n in SHUTTLE_NODES}
        declared = {f['node'] for f in d.get('ferry_landings', [])}
        if used != declared:
            fail(name, "uses ferry landings %s but declares %s"
                       % (sorted(used) or 'none', sorted(declared) or 'none'))
        # A no-ferry preset is the whole point of the axis: someone who will not
        # or cannot book the boat asked for an itinerary that never needs it.
        if ferry_ok is False and used:
            fail(name, "filename says no ferry, but the itinerary is collected "
                       "from %s" % sorted(used))

    # 2h. a walk has to start and finish somewhere a hiker can actually be
    #     dropped off or collected.  The solver states this rule itself as
    #     VALID_CIRCUIT_ENDPOINTS = trailhead + campground, but only applies it
    #     when picking the deadhead arc to remove; retarget_termini_for_budget
    #     later rotates the walk and drops an arc without rechecking, which is
    #     how the 8h open itinerary came to end at RI109, a road intersection.
    ends = [days[0]['start_node'], days[-1]['end_node']]
    for node in ends:
        if node[:2] not in ('TH', 'CG') and not (supported and node in SHUTTLE_NODES):
            fail(name, "walk terminus %s is a %s -- not a trailhead or "
                       "campground, so there is no way to start or finish "
                       "a hike there"
                       % (node, {'TI': 'trail intersection',
                                 'RI': 'road intersection',
                                 'BC': 'backcountry campsite',
                                 'SH': 'shelter'}.get(node[:2], 'unknown node')))

    if len(FAIL) == before:
        n_clean += 1
        if A.verbose:
            print("  OK   %-34s %d days" % (name, len(days)))
    else:
        print("  FAIL %-34s %d problem(s)" % (name, len(FAIL) - before))

print("  %d/%d presets clean (required trails: %d edges, %s mi)"
      % (n_clean, len(files), len(required_ids), required_miles))

# ---------------------------------------------------------------------------
print()
print("=" * 68)
if WARN:
    print("NOTES (%d)" % len(WARN))
    for cat, msg in WARN[:40]:
        print("  [%s] %s" % (cat, msg))
    if len(WARN) > 40:
        print("  ... and %d more" % (len(WARN) - 40))
    print("=" * 68)
if FAIL:
    print("FAILURES (%d)" % len(FAIL))
    for cat, msg in FAIL[:80]:
        print("  [%s] %s" % (cat, msg))
    if len(FAIL) > 80:
        print("  ... and %d more" % (len(FAIL) - 80))
    sys.exit(1)
print("ALL CHECKS PASSED")
