"""Delay from each node to the purchased material, in days, on the shortest path (docs/SPEC.md, section 5).

Edge u → v weighs the lead time of the edge plus the buffer stock of v, unless v is the purchased material.
Low (high) bound: each default buffer counts for half (one and a half) of its value; given values count as is.
`buffers` is the sum of the buffer stocks of the intermediate nodes on the central path: if an event lasts less
than that, its physical effect is absorbed before reaching the user.
"""

from __future__ import annotations

import itertools
from typing import Any

import networkx as nx


def _graph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], factor: float, sink: str) -> nx.DiGraph:
    by_key = {n["node_key"]: n for n in nodes}
    g = nx.DiGraph()
    g.add_nodes_from(by_key)
    for e in edges:
        a, b = e["from_key"], e["to_key"]
        if a not in by_key or b not in by_key:
            continue
        target = by_key[b]
        buffer = 0.0 if b == sink else float(target["buffer_days"] or 0)
        if target.get("buffer_is_default"):
            buffer *= factor
        g.add_edge(b, a, weight=float(e["lead_time_days"] or 0) + buffer, buffer=buffer)  # reversed graph
    return g


def delays_to_material(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """node_key -> {"days", "low", "high", "buffers"} for every node that reaches the purchased material."""
    sinks = [n["node_key"] for n in nodes if n["tier"] == 0]
    if len(sinks) != 1:
        return {}
    sink = sinks[0]
    result: dict[str, dict[str, float]] = {}
    for label, factor in (("days", 1.0), ("low", 0.5), ("high", 1.5)):
        g = _graph(nodes, edges, factor, sink)
        lengths, paths = nx.single_source_dijkstra(g, sink, weight="weight")
        for key, days in lengths.items():
            entry = result.setdefault(str(key), {})
            entry[label] = round(float(days), 1)
            if label == "days":
                path = paths[key]
                entry["buffers"] = round(sum(float(g[u][v]["buffer"]) for u, v in itertools.pairwise(path)), 1)
    return result
