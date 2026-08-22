"""Tobler's hiking function -- the one definition the whole project shares.

Until now this function existed nowhere in the codebase.  Hiking times were
computed once in a spreadsheet and pasted into the edge list as static columns,
which is why nothing could re-time a route: there was no function to call.  This
module is that function, and `build.py` emits the per-segment data that lets a
browser evaluate the same formula without a DEM.

    W(S) = v0 * exp(-k * |S - peak|)

  v0    speed on the flat-ish optimum, metres per hour   (Tobler: 6000)
  k     how sharply speed falls away from it             (Tobler: 3.5)
  peak  the slope where speed is highest -- slightly     (Tobler: -0.05)
        downhill, which is the function's whole point

Those three are the knobs a hiker should be able to turn: a fit backpacker on
good tread is a different v0, someone who suffers on climbs is a different k,
and the asymmetry hikers argue about lives in `peak`.

`slope_cap` is not one of the knobs.  It is a data-quality bound, not a
preference: bare earth reports the roof of Arch Rock where the Alum Cave trail
passes under it, and a 260% step there is the DEM disagreeing with the trail,
not a gradient anybody walks.  See profile.SLOPE_CAP.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_V0 = 6000.0        # metres per hour
DEFAULT_K = 3.5
DEFAULT_PEAK = -0.05       # rise/run


@dataclass(frozen=True)
class Tobler:
    v0: float = DEFAULT_V0
    k: float = DEFAULT_K
    peak: float = DEFAULT_PEAK

    def speed(self, slope: float) -> float:
        """Metres per hour at this slope (rise/run)."""
        return self.v0 * math.exp(-self.k * abs(slope - self.peak))

    def seconds(self, run_m: float, slope: float) -> float:
        return run_m / self.speed(slope) * 3600.0

    def time_from_histogram(self, bins, reverse: bool = False) -> float:
        """Seconds to walk a segment described by a slope histogram.

        `bins` maps a bin index to [distance_m, rise_m].  Each bin is timed at
        its OWN mean slope (rise / distance) rather than at the nominal bin
        centre, so the answer is nearly independent of how wide the bins are.

        Walking the other way negates every slope over the same distances, so
        the reverse time needs no second histogram -- just a sign flip.
        """
        total = 0.0
        for _b, (dist_m, rise_m) in bins.items():
            if dist_m <= 0:
                continue
            slope = rise_m / dist_m
            if reverse:
                slope = -slope
            total += dist_m / self.speed(slope) * 3600.0
        return total


DEFAULT = Tobler()

# The same function in JavaScript, for the browser to recompute times as a
# hiker moves the sliders.  Kept here so the two can never drift apart.
JS_SOURCE = """\
// Mirrors elevation/tobler.py -- keep in sync.
export function toblerSpeed(slope, p) {
  return p.v0 * Math.exp(-p.k * Math.abs(slope - p.peak));
}

// bins: { binIndex: [distanceMetres, riseMetres] }
export function timeFromHistogram(bins, p, reverse) {
  let total = 0;
  for (const key in bins) {
    const [dist, rise] = bins[key];
    if (dist <= 0) continue;
    const slope = (reverse ? -1 : 1) * (rise / dist);
    total += dist / toblerSpeed(slope, p) * 3600;
  }
  return total;
}

export const TOBLER_DEFAULTS = { v0: 6000, k: 3.5, peak: -0.05 };
"""
