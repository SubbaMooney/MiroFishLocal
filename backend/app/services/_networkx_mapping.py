"""
NetworkX -> Frontend/Service Schema-Mapping.

Geteilte Helper fuer Module, die strukturierte Reads aus LightRAGs
NetworkX-Storage in das vorhandene MiroFish-Schema (uuid/name/labels/
summary/fact/...) uebersetzen. LightRAG kennt zwei Repraesentationsformen
(direktes Dict vs. (id, data)-Tuple je nach Aufruf-Pfad), daher die
defensiven Lookup-Helper.

Konvention: ``uuid == entity_name`` — LightRAG kennt keine getrennten UUIDs,
der Name ist primaerer Identifier (siehe Phase-2-Migration-Doc).
"""

from __future__ import annotations

from typing import Any, Dict, List


def _node_get(node: Any, *keys: str, default: Any = None) -> Any:
    """LightRAG/NetworkX-Knoten kommen mal als Dict, mal als (id, data)-Tuple.
    Liefert den ersten vorhandenen Key zurueck."""
    if isinstance(node, tuple) and len(node) == 2 and isinstance(node[1], dict):
        node_id, data = node
        for k in keys:
            if k == "id":
                return node_id
            if k in data:
                return data[k]
        return default
    if isinstance(node, dict):
        for k in keys:
            if k in node:
                return node[k]
    return default


def _edge_get(edge: Any, *keys: str, default: Any = None) -> Any:
    """LightRAG-Edges kommen typischerweise als ((src, tgt), data)-Tuple
    oder als Dict. Liefert den ersten vorhandenen Key zurueck."""
    if isinstance(edge, tuple) and len(edge) == 2:
        endpoints, data = edge
        if isinstance(data, dict):
            if isinstance(endpoints, tuple) and len(endpoints) == 2:
                src, tgt = endpoints
                if "src_id" in keys or "source" in keys:
                    return src
                if "tgt_id" in keys or "target" in keys:
                    return tgt
            for k in keys:
                if k in data:
                    return data[k]
        return default
    if isinstance(edge, dict):
        for k in keys:
            if k in edge:
                return edge[k]
    return default


def node_to_dict(node: Any) -> Dict[str, Any]:
    """NetworkX-Knoten -> kanonisches Service-Schema.

    Schema:
      {uuid, name, labels, summary, attributes}

    ``uuid == entity_name`` (LightRAG-Konvention). Knoten ohne entity_name
    werden mit einem leeren ``name``-String emittiert; Caller filtern bei
    Bedarf.
    """
    entity_name = _node_get(node, "entity_name", "name", "id", default="") or ""
    entity_type = _node_get(node, "entity_type", "type", default="") or ""
    description = _node_get(node, "description", "summary", default="") or ""
    source_id = _node_get(node, "source_id", default="") or ""

    return {
        "uuid": entity_name,
        "name": entity_name,
        "labels": [entity_type] if entity_type else [],
        "summary": description,
        "attributes": {"source_id": source_id} if source_id else {},
    }


def edge_to_dict(edge: Any) -> Dict[str, Any]:
    """NetworkX-Edge -> kanonisches Service-Schema.

    Schema:
      {uuid, name, fact, source_node_uuid, target_node_uuid, attributes}

    ``uuid == "{src}__{tgt}"`` (LightRAG hat keine getrennten Edge-UUIDs).
    """
    src = _edge_get(edge, "src_id", "source", default="") or ""
    tgt = _edge_get(edge, "tgt_id", "target", default="") or ""
    description = _edge_get(edge, "description", default="") or ""
    keywords = _edge_get(edge, "keywords", default="") or ""
    weight = _edge_get(edge, "weight", default=None)
    source_id = _edge_get(edge, "source_id", default="") or ""

    attributes: Dict[str, Any] = {}
    if weight is not None:
        attributes["weight"] = weight
    if keywords:
        attributes["keywords"] = keywords
    if source_id:
        attributes["source_id"] = source_id

    return {
        "uuid": f"{src}__{tgt}" if src or tgt else "",
        "name": keywords or "",
        "fact": description,
        "source_node_uuid": src,
        "target_node_uuid": tgt,
        "attributes": attributes,
    }


def map_nodes(nodes: List[Any]) -> List[Dict[str, Any]]:
    """Wendet ``node_to_dict`` auf eine Liste an."""
    return [node_to_dict(n) for n in nodes]


def map_edges(edges: List[Any]) -> List[Dict[str, Any]]:
    """Wendet ``edge_to_dict`` auf eine Liste an."""
    return [edge_to_dict(e) for e in edges]


__all__ = [
    "_node_get",
    "_edge_get",
    "node_to_dict",
    "edge_to_dict",
    "map_nodes",
    "map_edges",
]
