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
# required trail is furthest from a pick-up point.  On roads alone that is
# Lakeshore, along Fontana Lake's north shore: 6.6 h from the nearest road at
# each end, making a road-only supported day 13.73 h at minimum.
#
# The Fontana Lake ferry landings attack exactly that trail -- minutes from the
# water where it is hours from tarmac -- and move the binding arc to the
# Appalachian Trail between Hughes Ridge and Tricorner Knob, 9.2 h pick-up to
# pick-up.  tools/road_bound.py recomputes both figures from the edge list if
# the network changes.
#
# Every day length the slider can reach now has an itinerary, so this floor no
# longer hides anything: at 8 h and 9 h a few days run past the budget and say
# so, which is a truer answer than refusing to plan.  The reason below is kept
# for a network change that puts a tier out of reach again.
SUPPORTED_MIN_HOURS = 8
SUPPORTED_REASON = (
    "Not possible at this day length. A supported hiker has to both begin and "
    "end every day where the crew can reach them, and the remotest required "
    "trail is further from a pick-up than this budget allows -- no way of "
    "arranging the days can change that. Try a longer day."
)

# What a *live* solve can manage, which is not the same as what the published
# grid offers.  Every supported day has to begin and end where a vehicle can
# reach, so the binding constraint is whichever required trail is furthest from
# a pick-up: Lakeshore at 13.7 h on roads alone, and the Appalachian Trail
# between Hughes Ridge and Tricorner Knob at 9.2 h once the ferry landings are
# available.  Below those a solve returns days nobody could walk.
#
# The published itineraries go lower than this because they are built
# differently -- they declare the days that run over rather than stretching one
# to absurdity -- which is exactly why the app has to warn before sending a
# short supported solve to the backend.  tools/carp_bound.py recomputes both.
SOLVER_FLOOR_HOURS = {"roads": 13.8, "ferry": 9.2}

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
            # Days no arrangement could fit inside the budget. The app has to
            # show this before someone plans around a number they cannot walk.
            entry["days_over_budget"] = len(d.get('days_over_budget', []))
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
                "solver_floor_hours": SOLVER_FLOOR_HOURS,
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
