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

# Measured, not guessed.  Every day of a supported trip has to both begin and
# end where the hiker can be collected, so the binding constraint is whichever
# required trail is furthest from a pick-up point.  That is Lakeshore, along
# Fontana Lake's north shore: 6.6 h from the nearest road at each end, making a
# road-only supported day 13.73 h at minimum.
#
# The Fontana Lake ferry landings attack exactly that trail -- minutes from the
# water where it is hours from tarmac -- and drop the floor to 9.36 h, which is
# why supported starts at 10 h rather than 14.  tools/road_bound.py recomputes
# both figures from the edge list if the network changes.
SUPPORTED_MIN_HOURS = 10
SUPPORTED_REASON = (
    "A supported hiker sleeps in town every night, so every day has to both "
    "begin and end somewhere the crew can reach. Even using the Fontana Lake "
    "ferry, the shortest possible supported day is 9.4 h -- set by Lakeshore "
    "Trail along the lake's north shore. Choose 10 h or longer, or switch to "
    "self-supported to sleep out there."
)

# Mirrors FERRY_LANDINGS in the solver.  Published so the custom-solve picker
# can offer them without a second hardcoded copy in the frontend.
FERRY_LANDINGS = [
    {"node": "TI051", "name": "Hazel Creek Access"},
    {"node": "TI053", "name": "Ollie Cove"},
    {"node": "TI064", "name": "Pilkey Creek"},
    {"node": "BC090", "name": "Campsite 90"},
]


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
            # Which landings this itinerary depends on, so the app can say what
            # has to be booked before anyone commits to it.
            entry["ferry_landings"] = d.get('ferry_landings', [])
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
                # Offered in the custom-solve panel.  The published presets take
                # them only where roads alone give no itinerary, since a ferry
                # is an expense and a schedule.
                "ferry_landings": FERRY_LANDINGS,
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
