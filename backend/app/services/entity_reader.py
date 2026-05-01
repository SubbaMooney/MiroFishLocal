"""
EntityReader — strukturierter Zugriff auf einen LightRAG-Graphen.

Phase 3 der Zep->LightRAG-Migration (siehe docs/MIGRATION-ZEP-TO-LIGHTRAG.md).

Drop-in-Ersatz fuer den alten ``ZepEntityReader``: gleiche Public-API,
gleiche Rueckgabe-Schemata (``EntityNode``, ``FilteredEntities``), aber
liest ueber ``RagManager`` direkt aus dem NetworkX-Storage — keine
Cloud-Calls, keine Pagination, keine Retries noetig (alles im RAM).

Caller (Phase-4-Module) bleiben unveraendert:
  - ``simulation_manager.py``
  - ``oasis_profile_generator.py``
  - ``simulation_config_generator.py``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from ..utils.logger import get_logger
from ._networkx_mapping import map_edges, map_nodes
from .rag_manager import RagManager

logger = get_logger("mirofish.entity_reader")


@dataclass
class EntityNode:
    """Implementierungsneutrale Entitaet — Schema unveraendert vs. Zep-Variante,
    damit Phase-4-Caller (oasis_profile_generator etc.) ohne Anpassung weiterlaufen."""
    uuid: str
    name: str
    labels: List[str]
    summary: str
    attributes: Dict[str, Any]
    related_edges: List[Dict[str, Any]] = field(default_factory=list)
    related_nodes: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "labels": self.labels,
            "summary": self.summary,
            "attributes": self.attributes,
            "related_edges": self.related_edges,
            "related_nodes": self.related_nodes,
        }

    def get_entity_type(self) -> Optional[str]:
        """Liefert das erste nicht-generische Label."""
        for label in self.labels:
            if label not in ("Entity", "Node"):
                return label
        return None


@dataclass
class FilteredEntities:
    """Aggregiertes Filter-Ergebnis — Schema unveraendert vs. Zep-Variante."""
    entities: List[EntityNode]
    entity_types: Set[str]
    total_count: int
    filtered_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entities": [e.to_dict() for e in self.entities],
            "entity_types": list(self.entity_types),
            "total_count": self.total_count,
            "filtered_count": self.filtered_count,
        }


class EntityReader:
    """Strukturierter Reader fuer LightRAG-Graphen.

    Konstruktor parameterlos — der Reader sitzt auf der ``RagManager``-
    Singleton-Instanz, die ihrerseits den langlebigen LightRAG-Loop verwaltet.
    """

    def __init__(self) -> None:
        self.rag = RagManager.get_instance()

    def get_all_nodes(self, graph_id: str) -> List[Dict[str, Any]]:
        """Alle Knoten als Dict-Liste im kanonischen Schema."""
        nodes = self.rag.get_all_nodes(graph_id)
        mapped = map_nodes(nodes)
        logger.info("EntityReader: graph=%s -> %d Knoten", graph_id, len(mapped))
        return mapped

    def get_all_edges(self, graph_id: str) -> List[Dict[str, Any]]:
        """Alle Edges als Dict-Liste im kanonischen Schema."""
        edges = self.rag.get_all_edges(graph_id)
        mapped = map_edges(edges)
        logger.info("EntityReader: graph=%s -> %d Edges", graph_id, len(mapped))
        return mapped

    def get_node_edges(self, graph_id: str, node_uuid: str) -> List[Dict[str, Any]]:
        """Alle Edges, an denen ein Knoten beteiligt ist (eingehend ODER ausgehend).

        Im Zep-Original war ``graph_id`` nicht erforderlich, weil Zep
        per-Knoten-Index hatte. NetworkX iteriert lokal — daher braucht es
        die graph_id, um die richtige Instanz anzusprechen.
        """
        all_edges = self.get_all_edges(graph_id)
        return [
            e for e in all_edges
            if e["source_node_uuid"] == node_uuid or e["target_node_uuid"] == node_uuid
        ]

    def filter_defined_entities(
        self,
        graph_id: str,
        defined_entity_types: Optional[List[str]] = None,
        enrich_with_edges: bool = True,
    ) -> FilteredEntities:
        """Filtert Knoten heraus, deren Labels ueber ``Entity``/``Node`` hinausgehen.

        Logik unveraendert vs. Zep-Variante:
        - Knoten mit ausschliesslich generischen Labels werden uebersprungen.
        - Wenn ``defined_entity_types`` gesetzt ist, muss ein Label matchen.
        - Mit ``enrich_with_edges=True`` werden ausgehende und eingehende
          Edges sowie die jeweils anderen Endknoten attached.
        """
        all_nodes = self.get_all_nodes(graph_id)
        total_count = len(all_nodes)
        all_edges = self.get_all_edges(graph_id) if enrich_with_edges else []

        node_map = {n["uuid"]: n for n in all_nodes}

        filtered: List[EntityNode] = []
        types_found: Set[str] = set()

        for node in all_nodes:
            labels: List[str] = node.get("labels", []) or []
            custom_labels = [l for l in labels if l not in ("Entity", "Node")]
            if not custom_labels:
                continue

            if defined_entity_types:
                matching = [l for l in custom_labels if l in defined_entity_types]
                if not matching:
                    continue
                entity_type = matching[0]
            else:
                entity_type = custom_labels[0]
            types_found.add(entity_type)

            entity = EntityNode(
                uuid=node["uuid"],
                name=node["name"],
                labels=labels,
                summary=node["summary"],
                attributes=node["attributes"],
            )

            if enrich_with_edges:
                related_edges: List[Dict[str, Any]] = []
                related_node_uuids: Set[str] = set()
                for edge in all_edges:
                    if edge["source_node_uuid"] == node["uuid"]:
                        related_edges.append({
                            "direction": "outgoing",
                            "edge_name": edge["name"],
                            "fact": edge["fact"],
                            "target_node_uuid": edge["target_node_uuid"],
                        })
                        related_node_uuids.add(edge["target_node_uuid"])
                    elif edge["target_node_uuid"] == node["uuid"]:
                        related_edges.append({
                            "direction": "incoming",
                            "edge_name": edge["name"],
                            "fact": edge["fact"],
                            "source_node_uuid": edge["source_node_uuid"],
                        })
                        related_node_uuids.add(edge["source_node_uuid"])
                entity.related_edges = related_edges

                related_nodes: List[Dict[str, Any]] = []
                for related_uuid in related_node_uuids:
                    rn = node_map.get(related_uuid)
                    if rn:
                        related_nodes.append({
                            "uuid": rn["uuid"],
                            "name": rn["name"],
                            "labels": rn["labels"],
                            "summary": rn.get("summary", ""),
                        })
                entity.related_nodes = related_nodes

            filtered.append(entity)

        logger.info(
            "EntityReader Filter: graph=%s, total=%d, filtered=%d, types=%s",
            graph_id, total_count, len(filtered), types_found,
        )

        return FilteredEntities(
            entities=filtered,
            entity_types=types_found,
            total_count=total_count,
            filtered_count=len(filtered),
        )

    def get_entity_with_context(
        self, graph_id: str, entity_uuid: str
    ) -> Optional[EntityNode]:
        """Einzelne Entitaet inkl. Edges + adjazenter Knoten."""
        all_nodes = self.get_all_nodes(graph_id)
        node_map = {n["uuid"]: n for n in all_nodes}

        target = node_map.get(entity_uuid)
        if not target:
            return None

        edges = self.get_node_edges(graph_id, entity_uuid)
        related_edges: List[Dict[str, Any]] = []
        related_node_uuids: Set[str] = set()
        for edge in edges:
            if edge["source_node_uuid"] == entity_uuid:
                related_edges.append({
                    "direction": "outgoing",
                    "edge_name": edge["name"],
                    "fact": edge["fact"],
                    "target_node_uuid": edge["target_node_uuid"],
                })
                related_node_uuids.add(edge["target_node_uuid"])
            else:
                related_edges.append({
                    "direction": "incoming",
                    "edge_name": edge["name"],
                    "fact": edge["fact"],
                    "source_node_uuid": edge["source_node_uuid"],
                })
                related_node_uuids.add(edge["source_node_uuid"])

        related_nodes: List[Dict[str, Any]] = []
        for related_uuid in related_node_uuids:
            rn = node_map.get(related_uuid)
            if rn:
                related_nodes.append({
                    "uuid": rn["uuid"],
                    "name": rn["name"],
                    "labels": rn["labels"],
                    "summary": rn.get("summary", ""),
                })

        return EntityNode(
            uuid=target["uuid"],
            name=target["name"],
            labels=target["labels"],
            summary=target["summary"],
            attributes=target["attributes"],
            related_edges=related_edges,
            related_nodes=related_nodes,
        )

    def get_entities_by_type(
        self,
        graph_id: str,
        entity_type: str,
        enrich_with_edges: bool = True,
    ) -> List[EntityNode]:
        """Convenience-Wrapper um ``filter_defined_entities``."""
        result = self.filter_defined_entities(
            graph_id=graph_id,
            defined_entity_types=[entity_type],
            enrich_with_edges=enrich_with_edges,
        )
        return result.entities


__all__ = ["EntityNode", "FilteredEntities", "EntityReader"]
