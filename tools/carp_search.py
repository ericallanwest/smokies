"""Phase 3: local search over the day assignment.

Phases 1 and 2 decide which edges share a day and then walk that day as well as
it can be walked.  What they cannot do is change their mind, and the day count
is decided almost entirely by the first of those.  This is the part that
changes its mind.

Two things run in alternation, because they want opposite things:

**Improvement** moves single edges between days, and swaps pairs, whenever that
walks less.  It is a gradient, and it never removes a day on its own.

**Elimination** picks the emptiest day and tries to place every one of its
edges somewhere else, accepting a worse total if it succeeds -- because day
count is the objective and total walking is only the tie-break.  This is the
move that actually pays.

Costs during the search come from splice arithmetic (remove an edge from a
route, insert it elsewhere), which is O(1) per trial.  Only an accepted move
pays for an exact re-route, so the search runs at the speed of the cheap
estimate and stores the exact answer.
"""
import random
import time

from carp_common import INF, day_optimum


def removal(net, route, pos):
    """Change in a route's cost from taking out the leg at pos (negative)."""
    tail, head, w = net.leg(route[pos])
    if len(route) == 1:
        return -(net.from_access.get(tail, INF) + w + net.to_access.get(head, INF))
    if pos == 0:
        nxt = net.leg(route[1])[0]
        return (net.from_access.get(nxt, INF)
                - net.from_access.get(tail, INF) - w - net.dist(head, nxt))
    if pos == len(route) - 1:
        prv = net.leg(route[-2])[1]
        return (net.to_access.get(prv, INF)
                - net.dist(prv, tail) - w - net.to_access.get(head, INF))
    prv, nxt = net.leg(route[pos - 1])[1], net.leg(route[pos + 1])[0]
    return net.dist(prv, nxt) - net.dist(prv, tail) - w - net.dist(head, nxt)


def insertion(net, route, item, pos):
    """Change in a route's cost from putting item in at pos (positive)."""
    tail, head, w = net.leg(item)
    if not route:
        return net.from_access.get(tail, INF) + w + net.to_access.get(head, INF)
    if pos == 0:
        nxt = net.leg(route[0])[0]
        return (net.from_access.get(tail, INF) + w + net.dist(head, nxt)
                - net.from_access.get(nxt, INF))
    if pos == len(route):
        prv = net.leg(route[-1])[1]
        return (net.dist(prv, tail) + w + net.to_access.get(head, INF)
                - net.to_access.get(prv, INF))
    prv, nxt = net.leg(route[pos - 1])[1], net.leg(route[pos])[0]
    return net.dist(prv, tail) + w + net.dist(head, nxt) - net.dist(prv, nxt)


def best_insertion(net, route, eid, budget=None, cost=None):
    """Cheapest way to add this edge to this route.  (delta, dir, pos) or None."""
    best = None
    for d in (0, 1):
        for pos in range(len(route) + 1):
            delta = insertion(net, route, (eid, d), pos)
            if delta >= INF:
                continue
            if budget is not None and cost + delta > budget:
                continue
            if best is None or delta < best[0]:
                best = (delta, d, pos)
    return best


def reroute(net, day, exact_max):
    """Re-walk a day exactly.  Cheap estimates drift; this pins them back."""
    if len(day['route']) <= exact_max:
        c, r = day_optimum(net, [e for e, _ in day['route']], exact_max)
        if c < day['seconds']:
            day['route'], day['seconds'] = r, c
    return day


def total(days):
    return sum(d['seconds'] for d in days)


def _bank(collect, *ds):
    """Record days as columns, keyed by the trails they cover."""
    if collect is None:
        return
    for d in ds:
        collect.setdefault(frozenset(e for e, _ in d['route']), list(d['route']))


def improve(net, days, budget, exact_max, deadline, collect=None):
    """Relocate and swap while it walks less.  Returns edges moved."""
    moved = 0
    changed = True
    while changed and time.time() < deadline:
        changed = False
        for i, src in enumerate(days):
            if len(src['route']) <= 1 or time.time() >= deadline:
                continue
            for pos in range(len(src['route'])):
                eid = src['route'][pos][0]
                gain = removal(net, src['route'], pos)
                best = None
                for j, dst in enumerate(days):
                    if i == j:
                        continue
                    cand = best_insertion(net, dst['route'], eid, budget,
                                          dst['seconds'])
                    if cand and (best is None or cand[0] < best[0]):
                        best = (cand[0], j, cand[1], cand[2])
                if best is None or gain + best[0] >= -1:
                    continue
                _, j, d, p = best
                src['route'].pop(pos)
                src['seconds'] += gain
                days[j]['route'].insert(p, (eid, d))
                days[j]['seconds'] += best[0]
                reroute(net, src, exact_max)
                reroute(net, days[j], exact_max)
                # Both ends of the move are new days that existed nowhere
                # before.  Banking them here rather than once a round is what
                # makes the column pool worth keeping: a rejected solution is
                # still made of usable days.
                _bank(collect, src, days[j])
                moved += 1
                changed = True
                break
    return moved


def eliminate(net, days, budget, exact_max, deadline, rng, collect=None):
    """Try to empty the smallest day into the others.  True if a day went."""
    order = sorted(range(len(days)), key=lambda i: (len(days[i]['route']),
                                                   days[i]['seconds']))
    for i in order[:max(3, len(days) // 8)]:
        if time.time() >= deadline:
            return False
        victim = days[i]
        others = [{'route': list(d['route']), 'seconds': d['seconds']}
                  for k, d in enumerate(days) if k != i]
        # Hardest edge first: if one cannot be placed the attempt is dead, and
        # finding that out early costs nothing.
        edges = sorted((e for e, _ in victim['route']),
                       key=lambda e: -net.solo(e))
        ok = True
        for eid in edges:
            best = None
            for j, dst in enumerate(others):
                cand = best_insertion(net, dst['route'], eid, budget,
                                      dst['seconds'])
                if cand and (best is None or cand[0] < best[0]):
                    best = (cand[0], j, cand[1], cand[2])
            if best is None:
                ok = False
                break
            _, j, d, p = best
            others[j]['route'].insert(p, (eid, d))
            others[j]['seconds'] += best[0]
        if ok:
            for d in others:
                reroute(net, d, exact_max)
            _bank(collect, *others)
            days[:] = others
            return True
    return False


def perturb(net, days, budget, exact_max, rng, strength=3):
    """Shake a few edges loose, so the next pass starts somewhere else."""
    for _ in range(strength):
        src = rng.choice([d for d in days if len(d['route']) > 1] or days)
        if len(src['route']) <= 1:
            continue
        pos = rng.randrange(len(src['route']))
        eid = src['route'][pos][0]
        gain = removal(net, src['route'], pos)
        pool = [d for d in days if d is not src]
        rng.shuffle(pool)
        for dst in pool:
            cand = best_insertion(net, dst['route'], eid, budget, dst['seconds'])
            if cand:
                src['route'].pop(pos)
                src['seconds'] += gain
                dst['route'].insert(cand[2], (eid, cand[1]))
                dst['seconds'] += cand[0]
                break


def search(net, days, budget, exact_max, seconds, seed=0, log=print,
           collect=None):
    """Run to a wall-clock budget.  Returns the best days found.

    Pass a dict as `collect` to keep every day the search builds, keyed by the
    set of trails it covers.  Almost all of them belong to solutions that were
    rejected, which is exactly why they are worth keeping: a day discarded here
    because its neighbours did not work out may be the day another tier needs,
    and tools/carp_pool.py can assemble an itinerary from days no single run
    ever held at once.
    """
    rng = random.Random(seed)
    deadline = time.time() + seconds
    days = [{'route': list(d['route']), 'seconds': d['seconds']} for d in days]
    for d in days:
        reroute(net, d, exact_max)
    best = [dict(route=list(d['route']), seconds=d['seconds']) for d in days]
    log(f"    start {len(days)} days, {total(days) / 3600:.1f} h")
    rounds = 0

    _bank(collect, *days)
    while time.time() < deadline:
        rounds += 1
        improve(net, days, budget, exact_max, deadline, collect)
        while eliminate(net, days, budget, exact_max, deadline, rng, collect):
            improve(net, days, budget, exact_max, deadline, collect)
            log(f"    -> {len(days)} days, {total(days) / 3600:.1f} h")
        if (len(days), total(days)) < (len(best), total(best)):
            best = [dict(route=list(d['route']), seconds=d['seconds'])
                    for d in days]
        else:
            days = [dict(route=list(d['route']), seconds=d['seconds'])
                    for d in best]
        perturb(net, days, budget, exact_max, rng)
    log(f"    {rounds} rounds, best {len(best)} days, {total(best) / 3600:.1f} h")
    for d in best:
        d['over'] = d['seconds'] > budget
    return best
