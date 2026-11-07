"""Closed project architecture graph parsing and conservative impact projection."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .storage import canonical_json_bytes, digest


ARCHITECTURE_GRAPH_RELATIVE_PATH = ".governance/architecture.graph.json"
ARCHITECTURE_GRAPH_SCHEMA_VERSION = "1.0"

_MAX_CANONICAL_BYTES = 1_048_576
_MAX_NODES = 256
_MAX_EDGES = 1024
_MAX_NODE_PATHS = 32
_MAX_NODE_GATES = 32
_MAX_ALWAYS_GATES = 32
_MAX_CHANGED_PATHS = 1024
_MAX_PATH_CHARS = 256
_MAX_TRAVERSAL_STEPS = _MAX_EDGES

_TOP_LEVEL_FIELDS = frozenset({"schema_version", "nodes", "edges", "always_gate_ids"})
_NODE_FIELDS = frozenset({"node_id", "kind", "path_prefixes", "owner", "gate_ids"})
_EDGE_FIELDS = frozenset({"dependent", "dependency", "kind"})
_NODE_KINDS = frozenset(
    {
        "application",
        "service",
        "library",
        "package",
        "module",
        "component",
        "data",
        "infrastructure",
        "test",
        "tooling",
    }
)
_STABLE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")


class ArchitectureGraphError(ValueError):
    """Raised when an architecture graph violates its closed contract."""


@dataclass(frozen=True)
class ArchitectureNode:
    node_id: str
    kind: str
    path_prefixes: tuple[str, ...]
    owner: str
    gate_ids: tuple[str, ...]


@dataclass(frozen=True)
class ArchitectureEdge:
    dependent: str
    dependency: str
    kind: str


@dataclass(frozen=True)
class ArchitectureGraph:
    schema_version: str
    nodes: tuple[ArchitectureNode, ...]
    edges: tuple[ArchitectureEdge, ...]
    always_gate_ids: tuple[str, ...]
    graph_sha256: str
    cycle_components: tuple[tuple[str, ...], ...]
    policy_gate_ids: tuple[str, ...]
    unknown_gate_ids: tuple[str, ...]

    @property
    def digest(self) -> str:
        return self.graph_sha256

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    @property
    def cycle_count(self) -> int:
        return len(self.cycle_components)

    @property
    def has_cycle(self) -> bool:
        return bool(self.cycle_components)

    @property
    def referenced_gate_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                set(self.always_gate_ids).union(
                    *(set(node.gate_ids) for node in self.nodes)
                )
            )
        )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ArchitectureGraphError(f"{label} must be a string-keyed object")
    return value


def _closed(value: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    missing = sorted(fields - set(value))
    unknown = sorted(set(value) - fields)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise ArchitectureGraphError(f"{label} fields are invalid: {'; '.join(details)}")


def _sequence(value: object, label: str, maximum: int, *, nonempty: bool = False) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ArchitectureGraphError(f"{label} must be an array")
    if nonempty and not value:
        raise ArchitectureGraphError(f"{label} must not be empty")
    if len(value) > maximum:
        raise ArchitectureGraphError(f"{label} exceeds its {maximum}-item bound")
    return value


def _stable_id(value: object, label: str) -> str:
    if type(value) is not str or not _STABLE_ID.fullmatch(value):
        raise ArchitectureGraphError(f"{label} must be a stable lowercase ID")
    return value


def _stable_ids(value: object, label: str, maximum: int) -> tuple[str, ...]:
    items = _sequence(value, label, maximum)
    result = tuple(_stable_id(item, f"{label}[{index}]") for index, item in enumerate(items))
    if len(set(result)) != len(result):
        raise ArchitectureGraphError(f"{label} contains duplicate IDs")
    return tuple(sorted(result))


def _policy_ids(value: object, label: str) -> tuple[str, ...]:
    items = _sequence(value, label, 4096)
    result = tuple(
        item
        for item in items
        if type(item) is str and item
    )
    if len(result) != len(items):
        raise ArchitectureGraphError(f"{label} must contain non-empty strings")
    if len(set(result)) != len(result):
        raise ArchitectureGraphError(f"{label} contains duplicate IDs")
    return tuple(sorted(result))


def _safe_relative(value: object, label: str, *, allow_root: bool, normalize_separator: bool) -> str:
    if type(value) is not str or not value or len(value) > _MAX_PATH_CHARS:
        raise ArchitectureGraphError(f"{label} must be a bounded non-empty path")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ArchitectureGraphError(f"{label} contains control characters")
    if not normalize_separator and "\\" in value:
        raise ArchitectureGraphError(f"{label} must use POSIX separators")
    normalized = value.replace("\\", "/") if normalize_separator else value
    if normalized == ".":
        if allow_root:
            return normalized
        raise ArchitectureGraphError(f"{label} must name a project-relative path")
    if (
        normalized.startswith("/")
        or normalized.endswith("/")
        or "//" in normalized
        or ":" in normalized
        or "?" in normalized
        or "#" in normalized
    ):
        raise ArchitectureGraphError(f"{label} must be a safe project-relative path")
    raw_parts = normalized.split("/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in raw_parts):
        raise ArchitectureGraphError(f"{label} must be traversal-free")
    return path.as_posix()


def _path_prefixes(value: object, label: str) -> tuple[str, ...]:
    items = _sequence(value, label, _MAX_NODE_PATHS, nonempty=True)
    prefixes = tuple(
        _safe_relative(
            item,
            f"{label}[{index}]",
            allow_root=True,
            normalize_separator=False,
        )
        for index, item in enumerate(items)
    )
    if len(set(prefixes)) != len(prefixes):
        raise ArchitectureGraphError(f"{label} contains duplicate prefixes")
    return tuple(sorted(prefixes))


def _canonical_graph_mapping(
    nodes: tuple[ArchitectureNode, ...],
    edges: tuple[ArchitectureEdge, ...],
    always_gate_ids: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema_version": ARCHITECTURE_GRAPH_SCHEMA_VERSION,
        "nodes": [
            {
                "node_id": node.node_id,
                "kind": node.kind,
                "path_prefixes": list(node.path_prefixes),
                "owner": node.owner,
                "gate_ids": list(node.gate_ids),
            }
            for node in nodes
        ],
        "edges": [
            {
                "dependent": edge.dependent,
                "dependency": edge.dependency,
                "kind": edge.kind,
            }
            for edge in edges
        ],
        "always_gate_ids": list(always_gate_ids),
    }


def _cycle_components(
    node_ids: tuple[str, ...], edges: tuple[ArchitectureEdge, ...]
) -> tuple[tuple[str, ...], ...]:
    adjacency = {node_id: set() for node_id in node_ids}
    self_edges: set[str] = set()
    for edge in edges:
        adjacency[edge.dependent].add(edge.dependency)
        if edge.dependent == edge.dependency:
            self_edges.add(edge.dependent)

    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node_id: str) -> None:
        nonlocal index
        indices[node_id] = index
        lowlinks[node_id] = index
        index += 1
        stack.append(node_id)
        on_stack.add(node_id)
        for dependency in sorted(adjacency[node_id]):
            if dependency not in indices:
                visit(dependency)
                lowlinks[node_id] = min(lowlinks[node_id], lowlinks[dependency])
            elif dependency in on_stack:
                lowlinks[node_id] = min(lowlinks[node_id], indices[dependency])
        if lowlinks[node_id] != indices[node_id]:
            return
        component = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node_id:
                break
        normalized = tuple(sorted(component))
        if len(normalized) > 1 or normalized[0] in self_edges:
            components.append(normalized)

    for node_id in node_ids:
        if node_id not in indices:
            visit(node_id)
    return tuple(sorted(components))


def parse_architecture_graph(
    value: object, *, policy_gate_ids: Sequence[str] = ()
) -> ArchitectureGraph:
    """Parse one in-memory graph mapping through the closed Version 1 contract."""
    mapping = _mapping(value, "architecture graph")
    try:
        encoded = canonical_json_bytes(mapping)
    except (TypeError, ValueError, RecursionError) as error:
        raise ArchitectureGraphError("architecture graph is not canonical JSON data") from error
    if len(encoded) > _MAX_CANONICAL_BYTES:
        raise ArchitectureGraphError("architecture graph exceeds the 1 MiB bound")
    _closed(mapping, _TOP_LEVEL_FIELDS, "architecture graph")
    if mapping["schema_version"] != ARCHITECTURE_GRAPH_SCHEMA_VERSION:
        raise ArchitectureGraphError("unsupported architecture graph schema version")

    raw_nodes = _sequence(mapping["nodes"], "nodes", _MAX_NODES, nonempty=True)
    nodes: list[ArchitectureNode] = []
    node_ids: set[str] = set()
    owned_prefixes: dict[str, str] = {}
    for index, raw_node in enumerate(raw_nodes):
        label = f"nodes[{index}]"
        item = _mapping(raw_node, label)
        _closed(item, _NODE_FIELDS, label)
        node_id = _stable_id(item["node_id"], f"{label}.node_id")
        if node_id in node_ids:
            raise ArchitectureGraphError("nodes contains duplicate node_id values")
        node_ids.add(node_id)
        kind = item["kind"]
        if type(kind) is not str or kind not in _NODE_KINDS:
            raise ArchitectureGraphError(f"{label}.kind is unsupported")
        prefixes = _path_prefixes(item["path_prefixes"], f"{label}.path_prefixes")
        for prefix in prefixes:
            if prefix in owned_prefixes:
                raise ArchitectureGraphError(
                    "path prefix ownership is duplicated across nodes"
                )
            owned_prefixes[prefix] = node_id
        nodes.append(
            ArchitectureNode(
                node_id=node_id,
                kind=kind,
                path_prefixes=prefixes,
                owner=_stable_id(item["owner"], f"{label}.owner"),
                gate_ids=_stable_ids(
                    item["gate_ids"], f"{label}.gate_ids", _MAX_NODE_GATES
                ),
            )
        )
    normalized_nodes = tuple(sorted(nodes, key=lambda item: item.node_id))

    raw_edges = _sequence(mapping["edges"], "edges", _MAX_EDGES)
    edges: list[ArchitectureEdge] = []
    edge_keys: set[tuple[str, str, str]] = set()
    for index, raw_edge in enumerate(raw_edges):
        label = f"edges[{index}]"
        item = _mapping(raw_edge, label)
        _closed(item, _EDGE_FIELDS, label)
        dependent = _stable_id(item["dependent"], f"{label}.dependent")
        dependency = _stable_id(item["dependency"], f"{label}.dependency")
        if dependent not in node_ids or dependency not in node_ids:
            raise ArchitectureGraphError(f"{label} contains a dangling node reference")
        if item["kind"] != "depends_on":
            raise ArchitectureGraphError(f"{label}.kind must be depends_on")
        key = (dependent, dependency, "depends_on")
        if key in edge_keys:
            raise ArchitectureGraphError("edges contains duplicate relationships")
        edge_keys.add(key)
        edges.append(ArchitectureEdge(*key))
    normalized_edges = tuple(
        sorted(edges, key=lambda item: (item.dependent, item.dependency, item.kind))
    )
    always_gate_ids = _stable_ids(
        mapping["always_gate_ids"], "always_gate_ids", _MAX_ALWAYS_GATES
    )
    configured_gate_ids = _policy_ids(policy_gate_ids, "policy_gate_ids")
    referenced_gate_ids = set(always_gate_ids)
    for node in normalized_nodes:
        referenced_gate_ids.update(node.gate_ids)
    canonical_mapping = _canonical_graph_mapping(
        normalized_nodes, normalized_edges, always_gate_ids
    )
    components = _cycle_components(
        tuple(node.node_id for node in normalized_nodes), normalized_edges
    )
    return ArchitectureGraph(
        schema_version=ARCHITECTURE_GRAPH_SCHEMA_VERSION,
        nodes=normalized_nodes,
        edges=normalized_edges,
        always_gate_ids=always_gate_ids,
        graph_sha256=digest(canonical_mapping),
        cycle_components=components,
        policy_gate_ids=configured_gate_ids,
        unknown_gate_ids=tuple(sorted(referenced_gate_ids - set(configured_gate_ids))),
    )


def architecture_graph_bytes(graph: ArchitectureGraph) -> bytes:
    """Serialize one parsed graph to its unique canonical Version 1 bytes."""
    if not isinstance(graph, ArchitectureGraph):
        raise TypeError("graph must be an ArchitectureGraph")
    return canonical_json_bytes(
        _canonical_graph_mapping(graph.nodes, graph.edges, graph.always_gate_ids)
    )


def _reject_duplicate_object_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArchitectureGraphError("architecture graph contains duplicate object fields")
        result[key] = value
    return result


def load_architecture_graph(
    root: str | Path, *, policy_gate_ids: Sequence[str] = ()
) -> ArchitectureGraph | None:
    """Load the optional canonical graph without modifying the project."""
    project_root = Path(root).resolve(strict=True)
    if not project_root.is_dir():
        raise ArchitectureGraphError("architecture graph root must be a directory")
    path = project_root / ARCHITECTURE_GRAPH_RELATIVE_PATH
    if not os.path.lexists(path):
        return None
    try:
        resolved_path = path.resolve(strict=True)
    except OSError as error:
        raise ArchitectureGraphError("architecture graph path cannot be resolved") from error
    if (
        path.is_symlink()
        or resolved_path != path
        or not resolved_path.is_relative_to(project_root)
        or not path.is_file()
    ):
        raise ArchitectureGraphError("architecture graph must be a regular project file")
    try:
        if path.stat().st_size > _MAX_CANONICAL_BYTES:
            raise ArchitectureGraphError("architecture graph exceeds the 1 MiB bound")
        payload = path.read_bytes()
        if not payload or len(payload) > _MAX_CANONICAL_BYTES:
            raise ArchitectureGraphError("architecture graph has invalid bounded bytes")
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_object_fields
        )
    except ArchitectureGraphError:
        raise
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise ArchitectureGraphError("architecture graph is not valid UTF-8 JSON") from error
    graph = parse_architecture_graph(value, policy_gate_ids=policy_gate_ids)
    try:
        if architecture_graph_bytes(graph) != payload:
            raise ArchitectureGraphError("architecture graph JSON is not canonical")
    except (TypeError, ValueError) as error:
        raise ArchitectureGraphError("architecture graph JSON is not canonical") from error
    return graph


def _prefix_contains(prefix: str, path: str) -> bool:
    return prefix == "." or path == prefix or path.startswith(prefix + "/")


def _path_contains(path: str, prefix: str) -> bool:
    return path == prefix or prefix.startswith(path + "/")


def _owners(graph: ArchitectureGraph, path: str) -> tuple[tuple[str, ...], bool]:
    matches: list[tuple[int, int, str]] = []
    for node in graph.nodes:
        for prefix in node.path_prefixes:
            if _prefix_contains(prefix, path):
                parts = 0 if prefix == "." else len(PurePosixPath(prefix).parts)
                matches.append((parts, len(prefix), node.node_id))
    owners: set[str] = set()
    if matches:
        specificity = max((parts, length) for parts, length, _ in matches)
        owners.update(
            node_id
            for parts, length, node_id in matches
            if (parts, length) == specificity
        )

    owners.update(
        node.node_id
        for node in graph.nodes
        for prefix in node.path_prefixes
        if prefix != "." and _path_contains(path, prefix)
    )
    normalized = tuple(sorted(owners))
    return normalized, len(normalized) > 1


def architecture_graph_impact(
    graph: ArchitectureGraph,
    changed_paths: Sequence[str],
    *,
    policy_gate_ids: Sequence[str] = (),
) -> Mapping[str, object]:
    """Project changed paths through longest ownership and reverse dependencies."""
    if not isinstance(graph, ArchitectureGraph):
        raise TypeError("graph must be an ArchitectureGraph")
    raw_paths = _sequence(changed_paths, "changed_paths", _MAX_CHANGED_PATHS)
    paths = tuple(
        sorted(
            {
                _safe_relative(
                    value,
                    f"changed_paths[{index}]",
                    allow_root=False,
                    normalize_separator=True,
                )
                for index, value in enumerate(raw_paths)
            }
        )
    )
    direct: set[str] = set()
    unmapped: list[str] = []
    ambiguous: list[str] = []
    for path in paths:
        owners, is_ambiguous = _owners(graph, path)
        if not owners:
            unmapped.append(path)
            continue
        direct.update(owners)
        if is_ambiguous:
            ambiguous.append(path)

    reverse = {node.node_id: set() for node in graph.nodes}
    for edge in graph.edges:
        reverse[edge.dependency].add(edge.dependent)
    affected = set(direct)
    queue = sorted(direct)
    traversal_steps = 0
    traversal_exhausted = False
    while queue and not traversal_exhausted:
        dependency = queue.pop(0)
        for dependent in sorted(reverse[dependency]):
            traversal_steps += 1
            if traversal_steps > _MAX_TRAVERSAL_STEPS:
                traversal_exhausted = True
                break
            if dependent not in affected:
                affected.add(dependent)
                queue.append(dependent)
                queue.sort()

    candidate_gate_ids = set(graph.always_gate_ids)
    for node in graph.nodes:
        if node.node_id in affected:
            candidate_gate_ids.update(node.gate_ids)
    configured = _policy_ids(policy_gate_ids, "policy_gate_ids")
    if not configured and graph.policy_gate_ids:
        configured = graph.policy_gate_ids
    unknown_gate_ids = tuple(
        sorted(set(graph.referenced_gate_ids) - set(configured))
    )
    fallback_full = bool(
        not direct
        or unmapped
        or ambiguous
        or graph.has_cycle
        or unknown_gate_ids
        or traversal_exhausted
    )
    return {
        "graph_sha256": graph.digest,
        "node_count": graph.node_count,
        "edge_count": graph.edge_count,
        "direct_node_ids": tuple(sorted(direct)),
        "affected_node_ids": tuple(sorted(affected)),
        "candidate_gate_ids": tuple(sorted(candidate_gate_ids)),
        "unmapped_paths": tuple(sorted(unmapped)),
        "ambiguous_paths": tuple(sorted(ambiguous)),
        "cycle_detected": graph.has_cycle,
        "cycle_count": graph.cycle_count,
        "unknown_gate_ids": unknown_gate_ids,
        "traversal_exhausted": traversal_exhausted,
        "fallback_full": fallback_full,
    }


__all__ = [
    "ARCHITECTURE_GRAPH_RELATIVE_PATH",
    "ARCHITECTURE_GRAPH_SCHEMA_VERSION",
    "ArchitectureEdge",
    "ArchitectureGraph",
    "ArchitectureGraphError",
    "ArchitectureNode",
    "architecture_graph_bytes",
    "architecture_graph_impact",
    "load_architecture_graph",
    "parse_architecture_graph",
]
