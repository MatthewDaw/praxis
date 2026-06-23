import type { Candidate } from "../types/candidate";
import type {
  GraphEdge,
  GraphEdgeKind,
  GraphNode,
  GraphSnapshotSource,
  KnowledgeGraphSnapshot,
  ScopeGroup,
} from "../types/graph";

const KNOWN_EDGE_KINDS = new Set<GraphEdgeKind>([
  "contradiction",
  "support",
  "similarity",
]);

function canonicalEdgeKey(src: string, dst: string, kind: GraphEdgeKind): string {
  const [a, b] = [src, dst].sort();
  return `${kind}:${a}__${b}`;
}

export function parseEdgeKind(raw: unknown): GraphEdgeKind {
  const label = String(raw ?? "contradiction");
  if (KNOWN_EDGE_KINDS.has(label as GraphEdgeKind)) {
    return label as GraphEdgeKind;
  }
  return "contradiction";
}

export function parseGraphNode(raw: Record<string, unknown>): GraphNode | null {
  const id = String(raw.id ?? "");
  if (!id) {
    return null;
  }
  return {
    id,
    label: String(raw.label ?? raw.title ?? id),
    state: parseNodeState(raw.state),
    confidence: Number(raw.confidence ?? 0),
    scope: raw.scope != null ? String(raw.scope) : undefined,
    category: raw.category != null ? String(raw.category) : undefined,
    provenance:
      raw.provenance != null
        ? String(raw.provenance)
        : raw.source != null
          ? String(raw.source)
          : undefined,
    clusterId:
      raw.clusterId != null
        ? Number(raw.clusterId)
        : raw.cluster_id != null
          ? Number(raw.cluster_id)
          : undefined,
    clusterLabel:
      raw.clusterLabel != null
        ? String(raw.clusterLabel)
        : raw.cluster_label != null
          ? String(raw.cluster_label)
          : undefined,
  };
}

function parseNodeState(raw: unknown): GraphNode["state"] {
  const label = String(raw ?? "proposed");
  if (
    label === "proposed" ||
    label === "active" ||
    label === "decayed" ||
    label === "unrecognized"
  ) {
    return label;
  }
  return "unrecognized";
}

export function parseGraphEdge(raw: Record<string, unknown>): GraphEdge | null {
  const src = String(raw.src ?? raw.source ?? "");
  const dst = String(raw.dst ?? raw.target ?? "");
  if (!src || !dst) {
    return null;
  }
  return {
    src,
    dst,
    kind: parseEdgeKind(raw.kind),
  };
}

export function parseScopeGroup(raw: Record<string, unknown>): ScopeGroup | null {
  const id = String(raw.id ?? "");
  if (!id) {
    return null;
  }
  const memberIds = Array.isArray(raw.memberIds)
    ? raw.memberIds.map((m) => String(m))
    : Array.isArray(raw.member_ids)
      ? raw.member_ids.map((m) => String(m))
      : [];
  const parentRaw = raw.parentId ?? raw.parent_id;
  return {
    id,
    label: String(raw.label ?? id),
    parentId: parentRaw == null || parentRaw === "" ? null : String(parentRaw),
    memberIds,
  };
}

export function parseGraphPayload(
  raw: unknown,
  source: GraphSnapshotSource = "api",
): KnowledgeGraphSnapshot {
  const root =
    raw && typeof raw === "object" && "graph" in raw
      ? (raw as Record<string, unknown>).graph
      : raw;
  if (!root || typeof root !== "object") {
    return { nodes: [], edges: [], source };
  }
  const record = root as Record<string, unknown>;
  const nodes = Array.isArray(record.nodes)
    ? record.nodes
        .map((n) =>
          n && typeof n === "object"
            ? parseGraphNode(n as Record<string, unknown>)
            : null,
        )
        .filter((n): n is GraphNode => n !== null)
    : [];
  const edges = Array.isArray(record.edges)
    ? record.edges
        .map((e) =>
          e && typeof e === "object"
            ? parseGraphEdge(e as Record<string, unknown>)
            : null,
        )
        .filter((e): e is GraphEdge => e !== null)
    : [];
  const scopeGroups = Array.isArray(record.scopeGroups)
    ? record.scopeGroups
        .map((g) =>
          g && typeof g === "object"
            ? parseScopeGroup(g as Record<string, unknown>)
            : null,
        )
        .filter((g): g is ScopeGroup => g !== null)
    : Array.isArray(record.scope_groups)
      ? record.scope_groups
          .map((g) =>
            g && typeof g === "object"
              ? parseScopeGroup(g as Record<string, unknown>)
              : null,
          )
          .filter((g): g is ScopeGroup => g !== null)
      : undefined;

  return {
    nodes,
    edges: dedupeEdges(edges),
    scopeGroups,
    source,
  };
}

export function dedupeEdges(edges: GraphEdge[]): GraphEdge[] {
  const seen = new Set<string>();
  const out: GraphEdge[] = [];
  for (const edge of edges) {
    const key = canonicalEdgeKey(edge.src, edge.dst, edge.kind);
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    out.push(edge);
  }
  return out;
}

export function deriveGraphFromCandidates(
  candidates: Candidate[],
): KnowledgeGraphSnapshot {
  const nodes: GraphNode[] = candidates.map((c) => ({
    id: c.id,
    label: c.title,
    state: c.state,
    confidence: c.confidence,
    scope:
      c.extra.scope != null ? String(c.extra.scope) : undefined,
    category:
      c.extra.category != null ? String(c.extra.category) : undefined,
    provenance: c.provenance,
    clusterId: c.extra.cluster_id != null ? Number(c.extra.cluster_id) : undefined,
    clusterLabel: c.extra.cluster_label != null ? String(c.extra.cluster_label) : undefined,
  }));

  const edges: GraphEdge[] = [];
  const seen = new Set<string>();
  for (const candidate of candidates) {
    for (const rivalId of candidate.contradictionIds) {
      const key = canonicalEdgeKey(candidate.id, rivalId, "contradiction");
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);
      edges.push({
        src: candidate.id,
        dst: rivalId,
        kind: "contradiction",
      });
    }
  }

  return {
    nodes,
    edges,
    source: "derived",
  };
}

export function mergeGraphWithCandidates(
  snapshot: KnowledgeGraphSnapshot,
  candidates: Candidate[],
): KnowledgeGraphSnapshot {
  const byId = new Map(candidates.map((c) => [c.id, c]));
  const derived = deriveGraphFromCandidates(candidates);
  const nodes = snapshot.nodes.map((node) => {
    const candidate = byId.get(node.id);
    if (!candidate) {
      return node;
    }
    return {
      ...node,
      label: candidate.title,
      state: candidate.state,
      confidence: candidate.confidence,
      provenance: candidate.provenance,
      scope:
        candidate.extra.scope != null
          ? String(candidate.extra.scope)
          : node.scope,
      category:
        candidate.extra.category != null
          ? String(candidate.extra.category)
          : node.category,
      clusterId:
        candidate.extra.cluster_id != null
          ? Number(candidate.extra.cluster_id)
          : node.clusterId,
      clusterLabel:
        candidate.extra.cluster_label != null
          ? String(candidate.extra.cluster_label)
          : node.clusterLabel,
    };
  });
  const nodeIds = new Set(nodes.map((node) => node.id));
  for (const node of derived.nodes) {
    if (!nodeIds.has(node.id)) {
      nodes.push(node);
      nodeIds.add(node.id);
    }
  }

  const derivedContradictions = new Set(
    derived.edges
      .filter((edge) => edge.kind === "contradiction")
      .map((edge) => canonicalEdgeKey(edge.src, edge.dst, edge.kind)),
  );
  const reconciledEdges = snapshot.edges.filter((edge) => {
    if (edge.kind !== "contradiction") {
      return true;
    }
    const touchesLoadedCandidate = byId.has(edge.src) || byId.has(edge.dst);
    if (!touchesLoadedCandidate) {
      return true;
    }
    return derivedContradictions.has(canonicalEdgeKey(edge.src, edge.dst, edge.kind));
  });

  return {
    ...snapshot,
    nodes,
    edges: dedupeEdges([...reconciledEdges, ...derived.edges]),
  };
}

export function cloneGraphSnapshot(
  snapshot: KnowledgeGraphSnapshot,
): KnowledgeGraphSnapshot {
  return {
    nodes: snapshot.nodes.map((n) => ({ ...n })),
    edges: snapshot.edges.map((e) => ({ ...e })),
    scopeGroups: snapshot.scopeGroups?.map((g) => ({
      ...g,
      memberIds: [...g.memberIds],
    })),
    source: snapshot.source,
  };
}
