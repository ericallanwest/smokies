"""Fetch driving times between the points a mid-day shuttle could run between.

A supported hiker can be driven mid-day, but only in place of a *deadhead run*
-- the walk still has to cover every required arc, so the only thing a van can
replace is a stretch that covers nothing and whose two ends the crew can reach.
That is what makes this affordable: the pairs worth pricing are not all 11,342
trailhead combinations but only those bounding such a run, which across the
published supported presets is 28.

Nothing is queried without --confirm.  A dry run reads the presets, works out
the pairs, prints what the call would cost at a rate you supply, and stops.

    # see the pairs and the price, no network access
    python tools/fetch_drive_times.py --rate-per-1000 5.00

    # actually fetch, filling in only what the cache is missing
    GOOGLE_MAPS_API_KEY=... python tools/fetch_drive_times.py \
        --rate-per-1000 5.00 --confirm

The rate is yours to supply from the Google Cloud console rather than baked in
here: Maps pricing was restructured in 2025 and how Routes calls land against
the free tier depends on the account.  A wrong number hardcoded in a tool is
worse than no number.

Results are cached in docs/data/drive_times.json and committed.  Trailheads do
not move, so this is paid once.
"""
import argparse
import json
import os
import sys
import time

FERRY = {'TI051', 'TI053', 'TI064', 'BC090'}
ENDPOINT = 'https://routes.googleapis.com/directions/v2:computeRoutes'


def collectible(node):
    """Somewhere a crew can reach: a trailhead, a campground, a ferry landing."""
    return node[:2] in ('TH', 'CG') or node in FERRY


def deadhead_runs(day_arcs):
    """Maximal runs of deadhead arcs, as (from_node, to_node, walk_seconds)."""
    out, i, n = [], 0, len(day_arcs)
    while i < n:
        if not day_arcs[i]['is_deadhead']:
            i += 1
            continue
        j = i
        while j + 1 < n and day_arcs[j + 1]['is_deadhead']:
            j += 1
        out.append((day_arcs[i]['from'], day_arcs[j]['to'],
                    sum(a['seconds'] for a in day_arcs[i:j + 1])))
        i = j + 1
    return out


def candidate_pairs(data_dir):
    """Every road-to-road deadhead run in the published supported itineraries.

    Boundary runs are included alongside interior ones: which is which depends
    on where the day split falls, and the split changes once shuttles exist.
    """
    import glob
    pairs, walk = {}, {}
    for fp in sorted(glob.glob(os.path.join(data_dir, 'preset_supported_*.json'))):
        with open(fp, encoding='utf-8') as f:
            d = json.load(f)
        for day in d['days']:
            for u, v, secs in deadhead_runs(day['arcs']):
                if u != v and collectible(u) and collectible(v):
                    pairs[(u, v)] = pairs.get((u, v), 0) + 1
                    walk[(u, v)] = max(walk.get((u, v), 0), secs)
    return pairs, walk


def load_points(data_dir):
    with open(os.path.join(data_dir, 'start_points.json'), encoding='utf-8') as f:
        pts = {p['id']: p for p in json.load(f)}
    # Ferry landings are not start points, so their coordinates come from the
    # map's node layer instead.
    gj = os.path.join(data_dir, 'points_20250211.geojson')
    if os.path.exists(gj):
        with open(gj, encoding='utf-8') as f:
            for feat in json.load(f)['features']:
                p = feat['properties']
                pts.setdefault(p['id'], {'id': p['id'], 'name': p.get('name', p['id']),
                                         'lat': p['latitude'], 'lon': p['longitude']})
    return pts


def fetch_one(key, a, b):
    """One driving route.  Returns seconds, or None if no route exists."""
    import urllib.request
    body = json.dumps({
        'origin': {'location': {'latLng': {'latitude': a['lat'], 'longitude': a['lon']}}},
        'destination': {'location': {'latLng': {'latitude': b['lat'], 'longitude': b['lon']}}},
        'travelMode': 'DRIVE',
        # Park roads are seasonal and some are one-way, but a support crew
        # plans around a typical day rather than live traffic.
        'routingPreference': 'TRAFFIC_UNAWARE',
    }).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        'Content-Type': 'application/json',
        'X-Goog-Api-Key': key,
        'X-Goog-FieldMask': 'routes.duration,routes.distanceMeters',
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    routes = payload.get('routes') or []
    if not routes:
        return None, None
    dur = routes[0]['duration']            # e.g. "3512s"
    return int(float(dur.rstrip('s'))), routes[0].get('distanceMeters')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default=os.path.join('docs', 'data'))
    ap.add_argument('--out', default=None,
                    help='default: <data>/drive_times.json')
    ap.add_argument('--rate-per-1000', type=float, required=True,
                    help='USD per 1000 Routes calls, from your own console')
    ap.add_argument('--confirm', action='store_true',
                    help='actually call the API; without this nothing is sent')
    ap.add_argument('--limit', type=int, default=None,
                    help='fetch at most N pairs this run')
    A = ap.parse_args()
    out_path = A.out or os.path.join(A.data, 'drive_times.json')

    pairs, walk = candidate_pairs(A.data)
    if not pairs:
        raise SystemExit(f"no supported presets in {A.data}; nothing to price")
    pts = load_points(A.data)

    cache = {}
    if os.path.exists(out_path):
        with open(out_path, encoding='utf-8') as f:
            cache = json.load(f).get('drive_seconds', {})

    missing, unlocatable = [], []
    for (u, v) in sorted(pairs):
        if f"{u}|{v}" in cache:
            continue
        if u not in pts or v not in pts:
            unlocatable.append((u, v))
            continue
        missing.append((u, v))
    if A.limit:
        missing = missing[:A.limit]

    print(f"pick-up pairs bounding a deadhead run : {len(pairs)}")
    print(f"  already cached                      : {len(pairs) - len(missing) - len(unlocatable)}")
    print(f"  no coordinates, skipped             : {len(unlocatable)}")
    print(f"  would be requested                  : {len(missing)}")
    cost = len(missing) * A.rate_per_1000 / 1000.0
    print(f"\n  1 Routes call per pair, at ${A.rate_per_1000:.2f}/1000")
    print(f"  ESTIMATED COST                      : ${cost:.2f}")
    print("  (your account's free tier may absorb this -- check the console;"
          "\n   this tool has no way to know and will not guess)")

    if missing[:10]:
        print("\nsample of what would be requested:")
        for u, v in missing[:10]:
            print(f"   {u} -> {v:<6} {pts[u]['name'][:26]:<26} -> "
                  f"{pts[v]['name'][:26]:<26} replaces {walk[(u, v)] / 60:5.0f} min walking")
    if unlocatable:
        print(f"\nno coordinates for: {sorted({n for p in unlocatable for n in p})}")

    if not A.confirm:
        print(f"\nDRY RUN -- nothing was sent. Re-run with --confirm to fetch.")
        return

    key = os.environ.get('GOOGLE_MAPS_API_KEY')
    if not key:
        raise SystemExit("set GOOGLE_MAPS_API_KEY to fetch")
    if not missing:
        print("\nnothing to fetch; cache is complete")
        return

    print(f"\nfetching {len(missing)} pair(s) ...")
    got = 0
    for i, (u, v) in enumerate(missing, 1):
        try:
            secs, metres = fetch_one(key, pts[u], pts[v])
        except Exception as exc:                      # noqa: BLE001
            # One bad pair must not cost the whole run: what has been paid for
            # is written out below either way.
            print(f"  [{i}/{len(missing)}] {u}->{v} FAILED: {exc}")
            continue
        if secs is None:
            print(f"  [{i}/{len(missing)}] {u}->{v} no driving route")
            continue
        cache[f"{u}|{v}"] = {'seconds': secs, 'metres': metres}
        got += 1
        print(f"  [{i}/{len(missing)}] {u}->{v} {secs / 60:.0f} min "
              f"(replaces {walk[(u, v)] / 60:.0f} min walking)")
        time.sleep(0.05)

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({'note': 'Driving seconds between crew pick-up points. '
                           'Built by tools/fetch_drive_times.py; trailheads do '
                           'not move, so this is fetched once and committed.',
                   'drive_seconds': dict(sorted(cache.items()))}, f, indent=1)
    print(f"\nwrote {out_path}: {got} new, {len(cache)} total")


if __name__ == '__main__':
    main()
