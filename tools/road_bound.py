"""The shortest day a supported hiker could possibly have.

A supported hiker is driven to a bed every night, so every required arc has to
fit inside one road-to-road day: (nearest road -> u) + the arc + (v -> nearest
road).  The largest of those over all required arcs is a hard floor on
--max-hours for --style supported, independent of how the walk is cut.

It is 13.73 h, set by Lakeshore Trail between campsites 81 and 77, which is
6.6 h from tarmac at either end.  That is why the supported presets start at
14 h and why anything shorter reports no itinerary rather than a bad one.

Re-run this whenever the edge list changes and update SUPPORTED_MIN_HOURS in
tools/build_presets_index.py to match.

    python tools/road_bound.py        # from the directory holding the edge list
"""
import csv, networkx as nx

rows = list(csv.DictReader(open('smokies_edge_list_20260509a.csv',
                                     encoding='utf-8-sig')))
G = nx.DiGraph()
for r in rows:
    if int(r.get('is_trail_closed') or 0):
        continue
    a, b = r['node_A'], r['node_B']
    for u, v, c in ((a, b, 'cost_A_to_B'), (b, a, 'cost_B_to_A')):
        w = int(r[c])
        if not G.has_edge(u, v) or G[u][v]['weight'] > w:
            G.add_edge(u, v, weight=w)

roads = {n for n in G if n[:2] == 'TH' or n[:2] == 'CG'}
out_of = nx.multi_source_dijkstra_path_length(G, roads, weight='weight')
Gr = G.reverse(copy=False)
back_of = nx.multi_source_dijkstra_path_length(Gr, roads, weight='weight')

worst = []
for r in rows:
    if int(r.get('is_trail_closed') or 0) or not int(r['is_required']):
        continue
    a, b = r['node_A'], r['node_B']
    for u, v, c in ((a, b, 'cost_A_to_B'), (b, a, 'cost_B_to_A')):
        if u in back_of and v in out_of:
            worst.append((back_of[u] + int(r[c]) + out_of[v],
                          r['Trail'], u, v))
worst.sort(reverse=True)
print(f"required arcs scored: {len(worst)}")
print(f"\nHardest required arcs to cover road-to-road:")
for s, t, u, v in worst[:6]:
    print(f"  {s/3600:6.2f} h   {t[:38]:<38} {u}->{v}")
print(f"\n=> a Supported day must be at least {worst[0][0]/3600:.2f} h")
