#!/usr/bin/env python3
"""
Generate dependency graph visualization with sort/anchor controls.
"""

import os
import re
import sys
import json
import argparse
from pathlib import Path

IMPORT_RE = re.compile(
    r"""(?x)
    (?:^|\s)
    (?:
        import \s+ (?:[^'"]*? \s+ from \s+)? ['"](?P<a>[^'"]+)['"]
        |
        import \s* \( \s* ['"](?P<b>[^'"]+)['"] \s* \)
    )
    """,
    re.MULTILINE,
)

def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    return src

def extract_imports(filepath):
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except (IOError, OSError):
        return []

    # Only process lines with no leading whitespace (top-level)
    top_level_src = "".join(line for line in lines if not line[0:1].isspace())
    top_level_src = strip_comments(top_level_src)
    return [m.group("a") or m.group("b") for m in IMPORT_RE.finditer(top_level_src) if m.group("a") or m.group("b")]
    
def file_metrics(filepath):
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"bytes": len(content.encode("utf-8")), "lines": content.count("\n") + 1}
    except (IOError, OSError):
        return {"bytes": 0, "lines": 0}

def resolve(import_path, current_file, project_root):
    if not import_path.startswith((".", "/")):
        return None
    base = Path(current_file).parent if import_path.startswith(".") else Path(project_root)
    candidate = (base / import_path).resolve()
    for p in [candidate, candidate.with_suffix(".js")]:
        if p.is_file():
            return p
    if candidate.is_dir():
        idx = candidate / "index.js"
        if idx.is_file():
            return idx
    if not str(candidate).endswith(".js"):
        p = Path(str(candidate) + ".js")
        if p.is_file():
            return p
    return None

def build_graph(entry, project_root):
    graph = {}
    queue = [entry]
    while queue:
        f = queue.pop()
        if f in graph:
            continue
        deps = []
        for imp in extract_imports(f):
            r = resolve(imp, f, project_root)
            if r is not None:
                deps.append(r)
                if r not in graph:
                    queue.append(r)
        graph[f] = deps
    return graph

def find_cycle_edges(graph):
    cycle_edges = set()
    state = {}
    stack = []

    def dfs(node):
        state[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if state.get(nxt, 0) == 1:
                idx = stack.index(nxt)
                cycle_path = stack[idx:] + [nxt]
                for i in range(len(cycle_path) - 1):
                    cycle_edges.add((cycle_path[i], cycle_path[i+1]))
            elif state.get(nxt, 0) == 0:
                dfs(nxt)
        stack.pop()
        state[node] = 2

    for node in graph:
        if state.get(node, 0) == 0:
            dfs(node)
    return cycle_edges

def find_all_cycles(graph, max_cycles_per_node=50):
    nodes = list(graph.keys())
    node_index = {n: i for i, n in enumerate(nodes)}
    all_cycles = []
    seen_canonical = set()

    def canonical(cycle):
        core = cycle[:-1]
        n = len(core)
        rotations = [tuple(core[i:] + core[:i]) for i in range(n)]
        return min(rotations)

    def dfs(start, current, path, visited):
        for nxt in graph.get(current, []):
            if nxt == start:
                cycle = path + [start]
                key = canonical(cycle)
                if key not in seen_canonical:
                    seen_canonical.add(key)
                    all_cycles.append(cycle)
            elif nxt not in visited and node_index[nxt] > node_index[start]:
                visited.add(nxt)
                path.append(nxt)
                dfs(start, nxt, path, visited)
                path.pop()
                visited.remove(nxt)

    for start in nodes:
        dfs(start, start, [start], {start})

    by_node = {n: [] for n in nodes}
    for cycle in all_cycles:
        members = set(cycle[:-1])
        for m in members:
            by_node[m].append(cycle)

    for n in by_node:
        by_node[n].sort(key=len)
        by_node[n] = by_node[n][:max_cycles_per_node]

    return by_node
def compute_metrics(entry, graph):
    nodes = list(graph.keys())

    depth_from_entry = {entry: 0}
    queue = [entry]
    while queue:
        n = queue.pop(0)
        for d in graph.get(n, []):
            if d not in depth_from_entry:
                depth_from_entry[d] = depth_from_entry[n] + 1
                queue.append(d)

    max_chain_down = {}
    def compute_down(node, visiting):
        if node in max_chain_down:
            return max_chain_down[node]
        if node in visiting:
            return 0
        visiting = visiting | {node}
        best = 0
        for d in graph.get(node, []):
            best = max(best, 1 + compute_down(d, visiting))
        max_chain_down[node] = best
        return best
    for n in nodes:
        compute_down(n, set())

    transitive = {}
    for n in nodes:
        seen = set()
        stack = [n]
        while stack:
            cur = stack.pop()
            for d in graph.get(cur, []):
                if d not in seen and d != n:
                    seen.add(d)
                    stack.append(d)
        transitive[n] = len(seen)

    out_count = {n: len(graph.get(n, [])) for n in nodes}
    in_count = {n: 0 for n in nodes}
    for src, deps in graph.items():
        for d in deps:
            in_count[d] = in_count.get(d, 0) + 1

    return {
        "depth_from_entry": depth_from_entry,
        "max_chain_down": max_chain_down,
        "transitive": transitive,
        "out_count": out_count,
        "in_count": in_count,
    }

def rel(p, root):
    return os.path.relpath(p, root)

def emit_html(entry, graph, cycle_edges, project_root, out):
    metrics = compute_metrics(entry, graph)

    nodes = []
    id_map = {}
    for i, p in enumerate(graph):
        id_map[p] = i
        fm = file_metrics(p)
        path_depth = len(Path(rel(p, project_root)).parts) - 1
        nodes.append({
            "id": i,
            "name": rel(p, project_root),
            "depthFromEntry": metrics["depth_from_entry"].get(p, 0),
            "maxChainDown": metrics["max_chain_down"].get(p, 0),
            "transitive": metrics["transitive"].get(p, 0),
            "outCount": metrics["out_count"].get(p, 0),
            "inCount": metrics["in_count"].get(p, 0),
            "fileBytes": fm["bytes"],
            "fileLines": fm["lines"],
            "pathDepth": path_depth,
        })

    links = []
    for src, deps in graph.items():
        for d in deps:
            links.append({
                "source": id_map[src],
                "target": id_map[d],
                "cycle": (src, d) in cycle_edges,
            })

    cycles_per_node = find_all_cycles(graph)
    cycles_data = {}
    for node_path, cycle_list in cycles_per_node.items():
        node_id = id_map[node_path]
        cycles_data[node_id] = [
            [id_map[step] for step in cycle] for cycle in cycle_list
        ]

    data = json.dumps({"nodes": nodes, "links": links, "cycles": cycles_data})

    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Dependency Graph</title>
<style>
body { margin: 0; background: #1a1a1a; font-family: monospace; color: #ccc; overflow: hidden; }
svg { width: 100vw; height: 100vh; }
.node circle { fill: #4a8; stroke: #2a6; stroke-width: 1.5px; cursor: pointer; }
.node text { fill: #ddd; font-size: 10px; pointer-events: none; }
.node.pinned circle { stroke: #fa0; stroke-width: 2.5px; }
.node.dimmed circle { opacity: 0.15; }
.node.dimmed { opacity: 1; }

.node.hovered circle { fill: #ff0; stroke: #fa0; r: 9; }
.node.hovered text { fill: #fff; font-size: 13px; font-weight: bold; }

.link { stroke: #555; stroke-opacity: 0.4; fill: none; }
.link.cycle { stroke: #f33; stroke-opacity: 0.9; stroke-width: 2px; }
.link.dimmed { stroke-opacity: 0.05; }
.link.tier-mode { stroke-opacity: 0.15; }
.link.tier-mode.cycle { stroke-opacity: 0.4; }
.link.highlighted { stroke: #660; stroke-opacity: 1; stroke-width: 1px; }

.cycles-panel { position: absolute; top: 10px; right: 10px; background: #2a2a2a; padding: 10px; border-radius: 4px; z-index: 10; width: 280px; max-height: 80vh; overflow-y: auto; transition: transform 0.3s ease; display: none; }
.cycles-panel.visible { display: block; }
.cycles-panel.hidden { transform: translateX(calc(100% - 40px)); }
.cycles-panel-toggle { position: absolute; top: 8px; left: 8px; background: #444; color: #ccc; border: 1px solid #555; width: 24px; height: 24px; cursor: pointer; font-family: monospace; font-size: 14px; border-radius: 3px; padding: 0; line-height: 22px; text-align: center; }
.cycles-panel-toggle:hover { background: #555; }
.cycles-panel h3 { margin: 0 0 8px 36px; font-size: 12px; color: #fa0; text-transform: uppercase; }
.cycles-panel .cycle { margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid #444; font-size: 11px; }
.cycles-panel .cycle:last-child { border-bottom: none; }
.cycles-panel .cycle-meta { color: #888; font-size: 10px; margin-bottom: 4px; }
.cycles-panel .cycle-step { padding: 2px 0; }
.cycles-panel .cycle-arrow { color: #f33; padding-left: 8px; }
.cycles-panel .cycle-step.origin { color: #ff0; font-weight: bold; }
.cycles-panel .cycle-step.other { color: #ccc; }



.controls { position: absolute; top: 10px; left: 10px; background: #2a2a2a; padding: 10px; border-radius: 4px; z-index: 10; max-width: 280px; }
.controls input { width: 200px; padding: 4px; background: #1a1a1a; color: #ccc; border: 1px solid #444; }
.controls .group { margin-top: 8px; padding-top: 8px; border-top: 1px solid #444; }
.controls .group-label { font-size: 10px; color: #888; margin-bottom: 4px; text-transform: uppercase; }
.controls button { background: #333; color: #ccc; border: 1px solid #555; padding: 4px 8px; margin: 2px; cursor: pointer; font-family: monospace; font-size: 11px; border-radius: 3px; }
.controls button:hover { background: #444; }
.controls button.active { background: #4a8; color: #000; border-color: #2a6; }
.controls button.toggle-on { background: #fa0; color: #000; border-color: #c80; }

.controls { transition: transform 0.3s ease; }
.controls.hidden { transform: translateX(calc(-100% + 40px)); }
.controls-toggle { position: absolute; top: 8px; right: 8px; background: #444; color: #ccc; border: 1px solid #555; width: 24px; height: 24px; cursor: pointer; font-family: monospace; font-size: 14px; border-radius: 3px; padding: 0; line-height: 22px; text-align: center; }
.controls-toggle:hover { background: #555; }

#info { margin-top: 6px; font-size: 11px; }
</style>
</head><body>
<div class="controls">
<button class="controls-toggle" id="controlsToggle">◀</button>
  <input type="text" id="search" placeholder="Filter nodes..." />
  <div class="group">
    <div class="group-label">Sort by chain length</div>
    <button data-sort="depthFromEntry">Depth from entry</button>
    <button data-sort="maxChainDown">Chain depth down</button>
    <button data-sort="transitive">Transitive deps</button>
  </div>
  <div class="group">
    <div class="group-label">Sort by imports</div>
    <button data-sort="inCount">Incoming</button>
    <button data-sort="outCount">Outgoing</button>
  </div>
  <div class="group">
    <div class="group-label">Sort by file</div>
    <button data-sort="fileBytes">File size</button>
    <button data-sort="fileLines">Lines</button>
    <button data-sort="pathDepth">Path depth</button>
    <button data-sort="name">Alphabetical</button>
  </div>
  <div class="group">
    <button id="freeLayout">Free layout</button>
    <button id="anchorAll">Anchor all: OFF</button>
    <button id="cyclesOnly">Cycles only: OFF</button>
  </div>
  <div id="info"></div>
  <div class="group">
    <div class="group-label">Tier layout</div>
    <label style="display:block; font-size:11px; margin:2px 0;">Max per row: <input type="number" id="maxPerRow" value="5" min="1" max="50" style="width:50px;"></label>
    <label style="display:block; font-size:11px; margin:2px 0;">Tier gap: <input type="number" id="tierGap" value="25" min="0" max="500" style="width:50px;"></label>
    <label style="display:block; font-size:11px; margin:2px 0;">Row gap: <input type="number" id="rowGap" value="20" min="0" max="500" style="width:50px;"></label>
    <label style="display:block; font-size:11px; margin:2px 0;">Col gap: <input type="number" id="colGap" value="250" min="0" max="1000" style="width:50px;"></label>
  </div>
</div>
<div class="cycles-panel" id="cyclesPanel">
  <button class="cycles-panel-toggle" id="cyclesPanelToggle">▶</button>
  <h3 id="cyclesPanelTitle">Cycles</h3>
  <div id="cyclesPanelBody"></div>
</div>
<svg></svg>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const data = __DATA__;
document.getElementById("info").textContent = data.nodes.length + " modules, " + data.links.length + " imports";

const svg = d3.select("svg");
const width = window.innerWidth, height = window.innerHeight;
const g = svg.append("g");

svg.call(d3.zoom().on("zoom", (e) => g.attr("transform", e.transform)));

const adj = new Map();
data.nodes.forEach(n => adj.set(n.id, new Set()));
data.links.forEach(l => {
    adj.get(l.source).add(l.target);
    adj.get(l.target).add(l.source);
});

const sim = d3.forceSimulation(data.nodes)
    .force("link", d3.forceLink(data.links).id(d => d.id).distance(90))
    .force("charge", d3.forceManyBody().strength(-300))
    .force("center", d3.forceCenter(width/2, height/2))
    .force("collide", d3.forceCollide(40));

const linkLayer = g.append("g");
const nodeLayer = g.append("g");

const link = linkLayer.selectAll("line")
    .data(data.links).join("line")
    .attr("class", d => "link" + (d.cycle ? " cycle" : ""));

const node = nodeLayer.selectAll("g")
    .data(data.nodes).join("g")
    .attr("class", "node")
    .call(d3.drag()
        .clickDistance(5)
        .on("start", (e,d) => {
            d._dragStartX = e.x;
            d._dragStartY = e.y;
            d._didDrag = false;
        })
        .on("drag", (e,d) => {
            const dx = e.x - d._dragStartX;
            const dy = e.y - d._dragStartY;
            if (!d._didDrag && (dx*dx + dy*dy) < 25) return;
            if (!d._didDrag) {
                d._didDrag = true;
                if (!anchorAllOn) sim.alphaTarget(0.3).restart();
            }
            d.fx = e.x;
            d.fy = e.y;
            updateNodePinClass();
        })
        .on("end", (e,d) => {
            if (!d._didDrag) return;
            if (!anchorAllOn) sim.alphaTarget(0);
            if (!anchorAllOn && !d._userPinned) {
                d.fx = null; d.fy = null;
            }
            updateNodePinClass();
        }));

node.append("circle").attr("r", 6);
node.append("text").attr("x", 9).attr("y", 4).text(d => d.name);
let clickHighlightedId = null;

// Cycles
const cyclesPanel = document.getElementById("cyclesPanel");
const cyclesPanelBody = document.getElementById("cyclesPanelBody");
const cyclesPanelTitle = document.getElementById("cyclesPanelTitle");
const cyclesPanelToggle = document.getElementById("cyclesPanelToggle");

cyclesPanelToggle.addEventListener("click", (e) => {
    e.stopPropagation();
    cyclesPanel.classList.toggle("hidden");
    cyclesPanelToggle.textContent = cyclesPanel.classList.contains("hidden") ? "◀" : "▶";
});

function renderCyclesPanel(nodeId) {
    const cycles = data.cycles[nodeId] || [];
    if (cycles.length === 0) {
        cyclesPanel.classList.remove("visible");
        return;
    }
    const node = data.nodes[nodeId];
    cyclesPanelTitle.textContent = `Cycles: ${node.name}`;
    cyclesPanelBody.innerHTML = "";

    cycles.forEach((cycle, i) => {
        const div = document.createElement("div");
        div.className = "cycle";
        const meta = document.createElement("div");
        meta.className = "cycle-meta";
        meta.textContent = `#${i + 1} · length ${cycle.length - 1}`;
        div.appendChild(meta);

        cycle.forEach((stepId, j) => {
            const step = document.createElement("div");
            step.className = "cycle-step " + (stepId === nodeId ? "origin" : "other");
            step.textContent = data.nodes[stepId].name;
            div.appendChild(step);
            if (j < cycle.length - 1) {
                const arrow = document.createElement("div");
                arrow.className = "cycle-arrow";
                arrow.textContent = "↓";
                div.appendChild(arrow);
            }
        });
        cyclesPanelBody.appendChild(div);
    });
    cyclesPanel.classList.add("visible");
}

function hideCyclesPanel() {
    cyclesPanel.classList.remove("visible");
}

function applyClickHighlight(id) {
    clickHighlightedId = id;
    if (id == null) {
        node.classed("dimmed", false);
        node.classed("hovered", false);
        link.classed("dimmed", false);
        link.classed("highlighted", false);
        return;
    }
    const neighbors = adj.get(id);
    node.classed("dimmed", n => n.id !== id && !neighbors.has(n.id));
    node.classed("hovered", n => n.id === id);
    link.classed("dimmed", l => (l.source.id !== id && l.target.id !== id));
    link.classed("highlighted", l => (l.source.id === id || l.target.id === id));
    nodeLayer.selectAll("g.node")
        .filter(n => n.id === id || neighbors.has(n.id))
        .raise();
    nodeLayer.selectAll("g.node").filter(n => n.id === id).raise();
}

node.on("click", function(event, d) {
    event.stopPropagation();
    if (clickHighlightedId === d.id) {
        applyClickHighlight(null);
        hideCyclesPanel();
    } else {
        applyClickHighlight(d.id);
        renderCyclesPanel(d.id);
    }
});

svg.on("click", () => { applyClickHighlight(null); hideCyclesPanel(); });


let anchorAllOn = false;
let activeSortKey = null;
let inTierMode = false;
let tierGapMultiplier = 1;

function updateNodePinClass() {
    node.classed("pinned", d => d.fx != null && d.fy != null);
}

sim.on("tick", () => {
    link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
    node.attr("transform", d => `translate(${d.x},${d.y})`);
});

const controlsEl = document.querySelector(".controls");
const controlsToggle = document.getElementById("controlsToggle");
controlsToggle.addEventListener("click", () => {
    controlsEl.classList.toggle("hidden");
    controlsToggle.textContent = controlsEl.classList.contains("hidden") ? "▶" : "◀";
});

function applyTierSort(sortKey) {
    activeSortKey = sortKey;
    inTierMode = true;
    link.classed("tier-mode", true);

    document.querySelectorAll(".controls button[data-sort]").forEach(b => {
        b.classList.toggle("active", b.dataset.sort === sortKey);
    });

    const sorted = [...data.nodes].sort((a, b) => {
        const av = a[sortKey], bv = b[sortKey];
        if (typeof av === "string") return bv.localeCompare(av);
        return bv - av;
    });

    const buckets = [];
    let currentBucket = [];
    let currentValue = null;
    for (const n of sorted) {
        if (currentValue === null || n[sortKey] === currentValue) {
            currentBucket.push(n);
            currentValue = n[sortKey];
        } else {
            buckets.push(currentBucket);
            currentBucket = [n];
            currentValue = n[sortKey];
        }
    }
    if (currentBucket.length) buckets.push(currentBucket);

    const MAX_PER_ROW = parseInt(document.getElementById("maxPerRow").value) || 5;
    const TIER_GAP = (parseInt(document.getElementById("tierGap").value) || 25) * tierGapMultiplier;
    const ROW_GAP = parseInt(document.getElementById("rowGap").value) || 20;
    const COL_GAP = parseInt(document.getElementById("colGap").value) || 250;

//    const MAX_PER_ROW = 5;
//    const TIER_GAP = 25 * tierGapMultiplier;
//    const ROW_GAP = 20;
//    const COL_GAP = 250;

    let totalHeight = 0;
    const tierLayouts = buckets.map(bucket => {
        const rows = Math.ceil(bucket.length / MAX_PER_ROW);
        const tierHeight = rows * ROW_GAP;
        totalHeight += tierHeight + TIER_GAP;
        return { bucket, rows, tierHeight };
    });
    totalHeight -= TIER_GAP;

    const startY = height - 80;
    let yCursor = startY;

    tierLayouts.forEach((layout, tierIdx) => {
        const { bucket, rows, tierHeight } = layout;
        const tierBottomY = yCursor;

        for (let i = 0; i < bucket.length; i++) {
            const row = Math.floor(i / MAX_PER_ROW);
            const col = i % MAX_PER_ROW;
            const rowCount = (row === rows - 1) ? (bucket.length - row * MAX_PER_ROW) : MAX_PER_ROW;
            const xCenter = width / 2;
            const xOffset = (col - (rowCount - 1) / 2) * COL_GAP;
            const targetX = xCenter + xOffset;
            const targetY = tierBottomY - row * ROW_GAP;

            const n = bucket[i];
            n.fx = targetX;
            n.fy = targetY;
            n._tierTargetX = targetX;
            n._tierTargetY = targetY;
        }

        yCursor -= (tierHeight + TIER_GAP);
    });

    sim.alpha(0).stop();
    node.transition()
        .duration(750)
        .ease(d3.easeCubicInOut)
        .attr("transform", d => `translate(${d.fx},${d.fy})`)
        .on("end", () => {
            data.nodes.forEach(n => { n.x = n.fx; n.y = n.fy; });
            link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
                .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
        });

    try{ 
        link.transition()
            .duration(750)
            .ease(d3.easeCubicInOut)
            .attr("x1", d => data.nodes[d.source.id || d.source].fx)
            .attr("y1", d => data.nodes[d.source.id || d.source].fy)
            .attr("x2", d => data.nodes[d.target.id || d.target].fx)
            .attr("y2", d => data.nodes[d.target.id || d.target].fy);
    } catch {}

    updateNodePinClass();
}

function freeLayout() {
    inTierMode = false;
    activeSortKey = null;
    link.classed("tier-mode", false);
    document.querySelectorAll(".controls button[data-sort]").forEach(b => b.classList.remove("active"));

    if (!anchorAllOn) {
        data.nodes.forEach(n => {
            if (!n._userPinned) {
                n.fx = null;
                n.fy = null;
            }
        });
    }
    sim.alpha(0.5).restart();
    updateNodePinClass();
}

document.querySelectorAll(".controls button[data-sort]").forEach(btn => {
    btn.addEventListener("click", () => applyTierSort(btn.dataset.sort));
});

document.getElementById("freeLayout").addEventListener("click", freeLayout);

const anchorBtn = document.getElementById("anchorAll");
anchorBtn.addEventListener("click", () => {
    anchorAllOn = !anchorAllOn;
    anchorBtn.textContent = "Anchor all: " + (anchorAllOn ? "ON" : "OFF");
    anchorBtn.classList.toggle("toggle-on", anchorAllOn);

    if (anchorAllOn) {
        sim.stop();
        data.nodes.forEach(n => {
            n.fx = n.x;
            n.fy = n.y;
        });
    } else {
        if (!inTierMode) {
            data.nodes.forEach(n => {
                if (!n._userPinned) {
                    n.fx = null;
                    n.fy = null;
                }
            });
            sim.alpha(0.3).restart();
        }
    }
    updateNodePinClass();
});

const cyclesOnlyBtn = document.getElementById("cyclesOnly");
let cyclesOnlyOn = false;

cyclesOnlyBtn.addEventListener("click", () => {
    cyclesOnlyOn = !cyclesOnlyOn;
    cyclesOnlyBtn.textContent = "Cycles only: " + (cyclesOnlyOn ? "ON" : "OFF");
    cyclesOnlyBtn.classList.toggle("toggle-on", cyclesOnlyOn);

    const cycleNodeIds = new Set();
    data.links.forEach(l => {
        if (l.cycle) {
            cycleNodeIds.add(l.source.id ?? l.source);
            cycleNodeIds.add(l.target.id ?? l.target);
        }
    });

    if (cyclesOnlyOn) {
        link.style("display", l => l.cycle ? null : "none");
        node.style("display", n => cycleNodeIds.has(n.id) ? null : "none");
    } else {
        link.style("display", null);
        node.style("display", null);
    }
});

function debounce(fn, ms) {
    let t;
    return (...args) => {
        clearTimeout(t);
        t = setTimeout(() => fn(...args), ms);
    };
}

const debouncedResort = debounce(() => {
    if (inTierMode && activeSortKey) applyTierSort(activeSortKey);
}, 250);

["maxPerRow", "tierGap", "rowGap", "colGap"].forEach(id => {
    document.getElementById(id).addEventListener("input", debouncedResort);
});

document.getElementById("search").addEventListener("input", (e) => {
    const q = e.target.value.toLowerCase();

    if (!q) {
        node.style("opacity", null);
        link.style("opacity", null);
    } else {
        // Find nodes whose names match the query
        const matchedIds = new Set(
            data.nodes.filter(n => n.name.toLowerCase().includes(q)).map(n => n.id)
        );

        // Find edges connected to matched nodes; respect cyclesOnly
        const visibleEdges = data.links.filter(l => {
            const s = l.source.id ?? l.source;
            const t = l.target.id ?? l.target;
            const connected = matchedIds.has(s) || matchedIds.has(t);
            if (!connected) return false;
            if (cyclesOnlyOn && !l.cycle) return false;
            return true;
        });

        // Nodes to keep bright: matched nodes + endpoints of visible edges
        const keepBrightIds = new Set(matchedIds);
        visibleEdges.forEach(l => {
            keepBrightIds.add(l.source.id ?? l.source);
            keepBrightIds.add(l.target.id ?? l.target);
        });

        node.style("opacity", d => keepBrightIds.has(d.id) ? 1 : 0.1);
        link.style("opacity", l => visibleEdges.includes(l) ? null : 0.05);
    }

    if (inTierMode && activeSortKey) {
        tierGapMultiplier = q ? 3 : 1;
        applyTierSort(activeSortKey);
    }
});
</script></body></html>
"""
    out.write(html.replace("__DATA__", data))

def main():
    p = argparse.ArgumentParser()
    p.add_argument("entry")
    p.add_argument("--root", default=None)
    p.add_argument("-o", "--output", default="deps.html")
    args = p.parse_args()

    entry = Path(args.entry).resolve()
    if not entry.is_file():
        print(f"ERROR: {entry} not found", file=sys.stderr)
        sys.exit(1)
    root = Path(args.root).resolve() if args.root else entry.parent

    graph = build_graph(entry, root)
    cycle_edges = find_cycle_edges(graph)

    with open(args.output, "w") as f:
        emit_html(entry, graph, cycle_edges, root, f)
    print(f"Wrote {args.output} ({len(graph)} modules, {sum(len(v) for v in graph.values())} edges, {len(cycle_edges)} cycle edges)", file=sys.stderr)

if __name__ == "__main__":
    main()
