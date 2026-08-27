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

Two axes were added once the grid could afford them: **pace**, the four Tobler
settings the app already offers as buttons, and for supported trips **ferry**,
which had been decided by publishing policy rather than by the hiker.  Both are
in the filename, and the old names are kept as aliases so a cached frontend
does not 404 mid-rollout -- see tools/publish_grid.py.

    python tools/build_presets_index.py [--data docs/data]
"""
import argparse
import glob
import json
import os
import re

SELF = re.compile(r'^preset_selfsup_(\d+)h(?:_r(\d+))?(?:_(\w+))?\.json$')
SUPP = re.compile(
    r'^preset_supported_(\d+)h(?:_(ferry|noferry))?(?:_(\w+))?\.json$')

HOURS = list(range(8, 17))             # 8..16, the slider's range
RESUPPLY = [4, 5, 6, 7, 8, None]       # None = unlimited, the slider's right edge

# The four pace buttons, with the Tobler constants each one stands for, so the
# custom panel and the preset grid quote the same numbers from one place.
# v0 is metres per hour on the flat, k the exponential's steepness, peak the
# slope at which walking is fastest -- slightly downhill, as it is for people.
PACES = [
    {"key": "heavy", "label": "Heavy pack", "v0": 5400.0, "k": 4.2,
     "peak": -0.05},
    {"key": "standard", "label": "Standard", "v0": 6000.0, "k": 3.5,
     "peak": -0.05},
    {"key": "strong", "label": "Strong", "v0": 6300.0, "k": 3.2,
     "peak": -0.05},
    {"key": "fast", "label": "Fast", "v0": 6600.0, "k": 3.0, "peak": -0.04},
]
DEFAULT_PACE = 'standard'

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
# Those are Standard-pace figures.  A slower pace scales them up, which is why
# Heavy pack has no 8 h supported itinerary at all and the grid, not this
# constant, decides what actually exists.
SOLVER_FLOOR_HOURS = {"roads": 13.8, "ferry": 9.2}

NO_FERRY_REASON = (
    "Not possible on roads alone at this day length. The remotest required "
    "trail is Lakeshore, on the north shore of Fontana Lake, and walking to it "
    "and back to a road takes longer than this budget allows. Allow the ferry, "
    "or try a longer day.")
FERRY_REASON = (
    "Not possible at this day length and pace. A supported hiker has to both "
    "begin and end every day where the crew can reach them, and the remotest "
    "required trail is further from a pick-up than this budget allows -- no "
    "way of arranging the days can change that. Try a longer day or a faster "
    "pace.")
SELF_REASON = (
    "No itinerary satisfies this combination of day length, resupply window "
    "and pace.")

# Mirrors FERRY_LANDINGS in the solver.  Published so the custom-solve picker
# can offer them without a second hardcoded copy in the frontend.
FERRY_LANDINGS = [
    {"node": "TI051", "name": "Hazel Creek Access"},
    {"node": "TI053", "name": "Ollie Cove"},
    {"node": "TI064", "name": "Pilkey Creek"},
    {"node": "BC090", "name": "Campsite 90"},
]


def self_key(h, rmax, pace):
    return (f"selfsup_{h}h" + (f"_r{rmax}" if rmax else "")
            + (f"_{pace}" if pace else ""))


def supp_key(h, ferry, pace):
    tag = "" if ferry is None else ("_ferry" if ferry else "_noferry")
    return f"supported_{h}h" + tag + (f"_{pace}" if pace else "")


def describe(fp):
    with open(fp, encoding='utf-8') as f:
        d = json.load(f)
    walk = sum(a['seconds'] for day in d['days'] for a in day['arcs'])
    return d, {
        "file": os.path.basename(fp),
        "days": d['n_days'],
        "walk_hours": round(walk / 3600.0, 1),
        "start_node": d.get('start_node'),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default=os.path.join('docs', 'data'))
    A = ap.parse_args()

    presets = {}
    for fp in sorted(glob.glob(os.path.join(A.data, 'preset_*.json'))):
        base = os.path.basename(fp)
        m = SELF.match(base)
        if m:
            _, entry = describe(fp)
            presets[self_key(int(m.group(1)), m.group(2), m.group(3))] = entry
            continue
        m = SUPP.match(base)
        if not m:
            print(f"  ignoring {base}: not a preset filename")
            continue
        d, entry = describe(fp)
        entry["shuttles"] = sum(
            1 for k in range(len(d['days']) - 1)
            if d['days'][k]['end_node'] != d['days'][k + 1]['start_node'])
        # Which landings this itinerary depends on, so the app can say what has
        # to be booked before anyone commits to it.
        entry["ferry_landings"] = d.get('ferry_landings', [])
        # Days no arrangement could fit inside the budget. The app has to show
        # this before someone plans around a number they cannot walk.
        entry["days_over_budget"] = len(d.get('days_over_budget', []))
        # A legacy filename carries no ferry flag; its landings say which it is.
        ferry = (m.group(2) == 'ferry') if m.group(2) else None
        presets[supp_key(int(m.group(1)), ferry, m.group(3))] = entry

    # Name every combination the controls can reach, so a gap is a statement
    # rather than a missing file.
    for pace in [p["key"] for p in PACES]:
        for h in HOURS:
            for rmax in RESUPPLY:
                presets.setdefault(self_key(h, rmax, pace),
                                   {"unavailable": SELF_REASON})
            for fer in (True, False):
                presets.setdefault(
                    supp_key(h, fer, pace),
                    {"unavailable": FERRY_REASON if fer else NO_FERRY_REASON})

    index = {
        "paces": PACES,
        "default_pace": DEFAULT_PACE,
        "styles": {
            "selfsup": {
                "label": "Self-supported",
                "hours": HOURS,
                "resupply": RESUPPLY,
                "ferry": [False],
            },
            "supported": {
                "label": "Supported",
                "hours": HOURS,
                "resupply": [None],
                "ferry": [True, False],
                "solver_floor_hours": SOLVER_FLOOR_HOURS,
                # Offered in the custom-solve panel, and now a published axis
                # too: an itinerary that needs a boat is a different trip from
                # one that does not, and the hiker gets to say which.
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
