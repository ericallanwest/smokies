"""Publish an index of the presets that actually exist.

The app used to rebuild a preset filename from the control values and fetch it,
which meant the label scheme was hand-copied into the frontend, both batch
tools and the validator -- four places to keep in step.  Worse, with sliders
replacing radios every combination is reachable in one drag, so a configuration
with no itinerary now shows up as a 404 rather than something the UI can
explain.

This scans whatever is in docs/data and writes the list, so the app looks a
configuration up instead of guessing at it, and can say *why* a missing one is
missing.  Run it after any batch that adds or replaces presets.

    python tools/build_presets_index.py [--data docs/data]
"""
import argparse
import glob
import json
import os
import re

PAT = re.compile(r'^preset_(selfsup|supported)_(\d+)h(?:_r(\d+))?\.json$')

HOURS    = list(range(8, 17))          # 8..16, the slider's range
RESUPPLY = [4, 5, 6, 7, 8, None]       # None = unlimited, the slider's right edge

# Measured, not guessed: the remotest required trail (Lakeshore, BC081<->BC077)
# sits 6.6 h from the nearest road at each end, so covering it road-to-road
# takes 13.73 h.  Below that no supported itinerary can exist at any day count,
# however the walk is cut.  tools/road_bound.py recomputes this from the edge
# list if the network changes.
SUPPORTED_MIN_HOURS = 14
SUPPORTED_REASON = (
    "A supported hiker sleeps in town every night, so every day has to both "
    "begin and end at a road. The remotest required trail -- Lakeshore, between "
    "campsites 81 and 77 -- is 6.6 h from the nearest road at each end, which "
    "makes the shortest possible supported day 13.7 h. Choose 14 h or longer, "
    "or switch to self-supported to sleep out there."
)


def key_for(style, hours, rmax):
    return f"{style}_{hours}h" + (f"_r{rmax}" if rmax else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default=os.path.join('docs', 'data'))
    A = ap.parse_args()

    presets = {}
    for fp in sorted(glob.glob(os.path.join(A.data, 'preset_*.json'))):
        m = PAT.match(os.path.basename(fp))
        if not m:
            print(f"  ignoring {os.path.basename(fp)}: not a preset filename")
            continue
        style, hours = m.group(1), int(m.group(2))
        rmax = int(m.group(3)) if m.group(3) else None
        with open(fp, encoding='utf-8') as f:
            d = json.load(f)
        walk = sum(a['seconds'] for day in d['days'] for a in day['arcs'])
        entry = {
            "file": os.path.basename(fp),
            "days": d['n_days'],
            "walk_hours": round(walk / 3600.0, 1),
            "start_node": d.get('start_node'),
        }
        if style == 'supported':
            entry["shuttles"] = sum(
                1 for k in range(len(d['days']) - 1)
                if d['days'][k]['end_node'] != d['days'][k + 1]['start_node'])
        presets[key_for(style, hours, rmax)] = entry

    # Name every combination the sliders can reach, so a gap is a statement
    # rather than a missing file.
    for hours in HOURS:
        for rmax in RESUPPLY:
            k = key_for('selfsup', hours, rmax)
            if k not in presets:
                presets[k] = {"unavailable":
                              "No itinerary satisfies this combination of day "
                              "length and resupply window."}
        k = key_for('supported', hours, None)
        if k not in presets:
            presets[k] = {"unavailable": SUPPORTED_REASON}

    index = {
        "styles": {
            "selfsup": {
                "label": "Self-supported",
                "hours": HOURS,
                "resupply": RESUPPLY,
            },
            "supported": {
                "label": "Supported",
                "hours": HOURS,
                "resupply": [None],
                "min_hours": SUPPORTED_MIN_HOURS,
            },
        },
        "presets": dict(sorted(presets.items())),
    }
    out = os.path.join(A.data, 'presets_index.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(index, f, indent=1)

    have = sum(1 for v in presets.values() if 'file' in v)
    print(f"wrote {out}: {have} presets, {len(presets) - have} gaps named")


if __name__ == '__main__':
    main()
