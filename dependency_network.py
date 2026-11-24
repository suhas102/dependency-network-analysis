"""
P3 - Dependency network analysis for a critical process
-------------------------------------------------------
Critical process: "Same-day delivery order fulfilment" for a fictional retailer.
Question: what does the process depend on, where are the single points of failure (SPOFs),
and what happens (and how fast do we need to recover) if each component fails?

DATA: a fictional dependency map built by hand below (names are generic, not any real
company's architecture). Replace the NODES/EDGES tables with real BIA output to reuse.
"""
import networkx as nx, pandas as pd, numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = "."
# type, owner, internal/external, recovery time objective (hours) the business needs
NODES = {
 "Same-day delivery fulfilment": ("process", "Ops", "internal", None),
 "Customer order capture":       ("process", "Digital", "internal", 1),
 "Payment authorisation":        ("process", "Finance", "internal", 1),
 "Stock allocation":             ("process", "Supply Chain", "internal", 2),
 "Pick and pack":                ("process", "Ops", "internal", 4),
 "Last-mile delivery":           ("process", "Ops", "internal", 4),
 "Website / app":                ("system", "Digital", "internal", 1),
 "Order management system":      ("system", "Digital", "internal", 2),
 "Stock system":                 ("system", "Supply Chain", "internal", 2),
 "Warehouse management system":  ("system", "Supply Chain", "internal", 4),
 "Payment gateway (3rd party)":  ("system", "Finance", "external", 1),
 "Cloud hosting region A":       ("infrastructure", "Tech", "external", 1),
 "Identity / SSO service":       ("infrastructure", "Tech", "external", 1),
 "Core network / WAN":           ("infrastructure", "Tech", "internal", 1),
 "Carrier A (3rd party)":        ("supplier", "Ops", "external", 4),
 "Carrier B (3rd party)":        ("supplier", "Ops", "external", 4),
 "Regional DC - North":          ("site", "Supply Chain", "internal", 8),
 "Regional DC - South":          ("site", "Supply Chain", "internal", 8),
 "Handheld scanner platform":    ("system", "Ops", "external", 4),
 "Key stock data supplier feed": ("supplier", "Supply Chain", "external", 4),
}
# edge A -> B means "A depends on B"
EDGES = [
 ("Same-day delivery fulfilment", "Customer order capture"), ("Same-day delivery fulfilment", "Payment authorisation"),
 ("Same-day delivery fulfilment", "Stock allocation"), ("Same-day delivery fulfilment", "Pick and pack"),
 ("Same-day delivery fulfilment", "Last-mile delivery"),
 ("Customer order capture", "Website / app"), ("Customer order capture", "Order management system"),
 ("Payment authorisation", "Payment gateway (3rd party)"), ("Payment authorisation", "Order management system"),
 ("Stock allocation", "Stock system"), ("Stock allocation", "Key stock data supplier feed"),
 ("Pick and pack", "Warehouse management system"), ("Pick and pack", "Handheld scanner platform"),
 ("Pick and pack", "Regional DC - North"),                       # single DC for same-day (design weakness)
 ("Last-mile delivery", "Carrier A (3rd party)"), ("Last-mile delivery", "Carrier B (3rd party)"),
 ("Website / app", "Cloud hosting region A"), ("Website / app", "Identity / SSO service"),
 ("Order management system", "Cloud hosting region A"), ("Order management system", "Core network / WAN"),
 ("Stock system", "Cloud hosting region A"), ("Stock system", "Core network / WAN"),
 ("Warehouse management system", "Core network / WAN"), ("Warehouse management system", "Stock system"),
 ("Handheld scanner platform", "Identity / SSO service"),
 ("Payment gateway (3rd party)", "Cloud hosting region A"),     # concentration: gateway also on region A
 ("Regional DC - North", "Core network / WAN"),
]
# "AND" vs "OR": a process with redundant providers survives losing one
REDUNDANT_GROUPS = {"Last-mile delivery": ["Carrier A (3rd party)", "Carrier B (3rd party)"]}

G = nx.DiGraph()
for n, (t, o, loc, rto) in NODES.items():
    G.add_node(n, type=t, owner=o, location=loc, rto=rto)
G.add_edges_from(EDGES)
assert nx.is_directed_acyclic_graph(G)
ROOT = "Same-day delivery fulfilment"

def failed_set(failed):
    """Return all nodes that stop working if `failed` node(s) fail (propagates upward through dependencies)."""
    down = set(failed)
    changed = True
    while changed:
        changed = False
        for n in G.nodes:
            if n in down: continue
            deps = list(G.successors(n))
            if not deps: continue
            if n in REDUNDANT_GROUPS:
                red = REDUNDANT_GROUPS[n]
                alive_red = [r for r in red if r not in down]
                other = [d for d in deps if d not in red]
                if (not alive_red) or any(d in down for d in other):
                    down.add(n); changed = True
            elif any(d in down for d in deps):
                down.add(n); changed = True
    return down

# ----------------------------------------------------------- 1. single-point-of-failure scan
rows = []
for n in G.nodes:
    if n == ROOT: continue
    down = failed_set([n])
    processes_hit = [x for x in down if G.nodes[x]["type"] == "process" and x != ROOT]
    rows.append(dict(component=n, type=G.nodes[n]["type"], owner=G.nodes[n]["owner"], location=G.nodes[n]["location"],
                     rto_hours=G.nodes[n]["rto"], dependants=len(nx.ancestors(G, n)),
                     components_impacted=len(down) - 1, steps_down=len(processes_hit),
                     breaks_root_process=ROOT in down,
                     out_degree_deps=G.out_degree(n)))
spof = pd.DataFrame(rows).sort_values(["breaks_root_process", "components_impacted"], ascending=False)
spof["priority"] = np.where(spof.breaks_root_process & (spof.location == "external"), "1 - SPOF, 3rd party",
                    np.where(spof.breaks_root_process, "2 - SPOF, internal",
                    np.where(spof.components_impacted >= 4, "3 - high blast radius", "4 - monitor")))
spof = spof[spof.type != "process"]          # process steps are trivially critical; rank the things that support them
spof.to_csv(f"{OUT}/spof_ranking.csv", index=False)

# ----------------------------------------------------------- 2. concentration risk
conc = []
for n in G.nodes:
    if G.nodes[n]["location"] == "external" and G.nodes[n]["type"] in ("infrastructure", "supplier", "system"):
        conc.append((n, len(nx.ancestors(G, n))))
conc = pd.DataFrame(conc, columns=["third_party", "upstream_dependants"]).sort_values("upstream_dependants", ascending=False)
conc.to_csv(f"{OUT}/third_party_concentration.csv", index=False)

# ----------------------------------------------------------- 3. two-failure stress test
import itertools
cands = [n for n in G.nodes if n != ROOT]
pair_rows = []
for a, b in itertools.combinations(cands, 2):
    d = failed_set([a, b])
    base_a, base_b = ROOT in failed_set([a]), ROOT in failed_set([b])
    if ROOT in d and not (base_a or base_b):                     # only the COMBINATION breaks the process
        pair_rows.append((a, b))
pairs = pd.DataFrame(pair_rows, columns=["failure_1", "failure_2"])
pairs.to_csv(f"{OUT}/compound_failures.csv", index=False)

# ----------------------------------------------------------- 4. RTO conflicts
rto_rows = []
for u, v in G.edges:
    ru, rv = G.nodes[u]["rto"], G.nodes[v]["rto"]
    if ru is not None and rv is not None and rv > ru:
        rto_rows.append(dict(depends_on_slower=u, supports=v, needs_recovery_h=ru, dependency_rto_h=rv))
rto = pd.DataFrame(rto_rows)
rto.to_csv(f"{OUT}/rto_conflicts.csv", index=False)

# ----------------------------------------------------------- 5. diagram
pos = {}
layers = {"process": 0, "system": 1, "infrastructure": 2, "supplier": 1, "site": 2}
by_layer = {}
reach = nx.descendants(G, ROOT) | {ROOT}
orphans = [n for n in G.nodes if n not in reach]       # in the inventory but not wired into this process
depth = {n: (nx.shortest_path_length(G, ROOT, n) if n in reach else 4) for n in G.nodes}
for n, d in depth.items(): by_layer.setdefault(d, []).append(n)
for d, nodes in by_layer.items():
    for i, n in enumerate(sorted(nodes, key=lambda x: (G.nodes[x]["type"], x))):
        pos[n] = ((i + 0.5) / len(nodes) * 20, -d * 3 - (0.9 if i % 2 else 0))
col = {"process": "#1f3b73", "system": "#2e7d32", "infrastructure": "#6a1b9a", "supplier": "#e07b00", "site": "#00838f"}
sp_break = set(spof[spof.breaks_root_process].component)
fig, ax = plt.subplots(figsize=(20, 9))
nx.draw_networkx_edges(G, pos, ax=ax, arrowsize=10, edge_color="#9e9e9e", width=.9, node_size=700)
nx.draw_networkx_nodes(G, pos, ax=ax, node_size=700, node_color=[col[G.nodes[n]["type"]] for n in G.nodes],
                       edgecolors=["#d50000" if n in sp_break else "white" for n in G.nodes],
                       linewidths=[4 if n in sp_break else 1 for n in G.nodes])
nx.draw_networkx_labels(G, pos, ax=ax, font_size=7.5, font_color="black", verticalalignment="top", labels={n: n.replace(" (3rd party)", "\n(3rd party)").replace(" / ", "/\n") for n in G.nodes}, bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#bdbdbd", alpha=.92))
ax.set_title("Dependency map: Same-day delivery fulfilment (red outline = single point of failure for the whole process)", fontsize=13)
from matplotlib.lines import Line2D
ax.legend(handles=[Line2D([0], [0], marker="o", color="w", markerfacecolor=c, markersize=12, label=k) for k, c in col.items()], loc="lower left")
ax.axis("off"); plt.tight_layout(); plt.savefig(f"{OUT}/dependency_map.png", dpi=140); plt.close()

# impact bar chart
top = spof.head(12).iloc[::-1]
fig, ax = plt.subplots(figsize=(10, 6))
ax.barh(top.component, top.components_impacted, color=["#c62828" if b else "#607d8b" for b in top.breaks_root_process])
ax.set_xlabel("Other components knocked out if this fails"); ax.set_title("Blast radius by component (red = breaks whole process)")
plt.tight_layout(); plt.savefig(f"{OUT}/blast_radius.png", dpi=140); plt.close()

print("Components not connected to the process (possible unused failover):", orphans)
print("\nSingle points of failure (break whole process):")
print(spof[spof.breaks_root_process][["component", "location", "components_impacted", "rto_hours", "priority"]].to_string(index=False))
print("\nThird-party concentration:\n", conc.to_string(index=False))
print(f"\nCompound failures (pairs that break process only together): {len(pairs)}")
print(pairs.head(6).to_string(index=False))
print("\nRTO conflicts (dependency slower to recover than the thing relying on it):\n", rto.to_string(index=False))
