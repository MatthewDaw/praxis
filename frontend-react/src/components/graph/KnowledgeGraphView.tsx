import { memo, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  useNodesInitialized,
  useReactFlow,
  useStore,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { CandidateState } from "../../types/candidate";
import type {
  GraphEdgeKind,
  KnowledgeGraphSnapshot,
  TicketBuildState,
} from "../../types/graph";
import { EDGE_LEGEND } from "../viz";
import { GraphLegend } from "./GraphLegend";
import {
  getTopCenterViewport,
  layoutDagNodes,
  layoutGraphNodes,
  stateNodeColors,
} from "./graphLayout";

interface CandidateNodeData extends Record<string, unknown> {
  label: string;
  state: CandidateState;
  confidence: number;
  buildState?: TicketBuildState;
}

// Ticket done-state badge copy: symbol + short label per build_state.
const TICKET_BADGE: Record<TicketBuildState, { symbol: string; label: string }> = {
  finished: { symbol: "✓", label: "Done" },
  in_progress: { symbol: "⟳", label: "In progress" },
  blocked: { symbol: "✕", label: "Blocked" },
  incomplete: { symbol: "○", label: "To do" },
};

function CandidateGraphNode({ data, selected }: NodeProps<Node<CandidateNodeData>>) {
  const colors = stateNodeColors(data.state);
  const badge = data.buildState ? TICKET_BADGE[data.buildState] : null;
  return (
    <div
      className="graph-node"
      style={{
        background: colors.bg,
        color: colors.text,
        borderColor: colors.border,
        boxShadow: selected ? "0 0 0 2px var(--accent)" : undefined,
      }}
    >
      <Handle type="target" position={Position.Top} className="graph-node__handle" />
      {badge ? (
        <span
          className={`graph-node__ticket graph-node__ticket--${data.buildState}`}
          title={`Ticket: ${badge.label}`}
        >
          <span aria-hidden="true">{badge.symbol}</span> {badge.label}
        </span>
      ) : null}
      <p className="graph-node__title">{data.label}</p>
      <p className="graph-node__meta">
        {data.state} · {Math.round(data.confidence * 100)}%
      </p>
      <Handle type="source" position={Position.Bottom} className="graph-node__handle" />
    </div>
  );
}

interface ClusterNodeData extends Record<string, unknown> {
  label: string;
  count: number;
  expanded: boolean;
}

function ClusterSuperNode({ data, selected }: NodeProps<Node<ClusterNodeData>>) {
  return (
    <div
      className="graph-cluster-node"
      style={{ boxShadow: selected ? "0 0 0 2px var(--accent)" : undefined }}
    >
      <Handle type="target" position={Position.Top} className="graph-node__handle" />
      <p className="graph-cluster-node__label">
        {data.expanded ? "▾" : "▸"} {data.label || "Topic"}
      </p>
      <p className="graph-cluster-node__count">{data.count} facts</p>
      <Handle type="source" position={Position.Bottom} className="graph-node__handle" />
    </div>
  );
}

const nodeTypes = {
  candidateNode: CandidateGraphNode,
  clusterNode: ClusterSuperNode,
};

const UNCLUSTERED = "__unclustered__";

// Group nodes by clusterId; unclustered (null/undefined) collected separately.
function groupByCluster(nodes: KnowledgeGraphSnapshot["nodes"]) {
  const clusters = new Map<number, { label: string; members: typeof nodes }>();
  const unclustered: typeof nodes = [];
  for (const node of nodes) {
    if (node.clusterId == null) {
      unclustered.push(node);
      continue;
    }
    const entry = clusters.get(node.clusterId) ?? { label: node.clusterLabel ?? "", members: [] };
    entry.members.push(node);
    if (node.clusterLabel) entry.label = node.clusterLabel;
    clusters.set(node.clusterId, entry);
  }
  return { clusters, unclustered };
}

function AlignGraphViewTopCenter({
  graphKey,
  nodeCount,
  edgeCount,
}: {
  graphKey: string;
  nodeCount: number;
  edgeCount: number;
}) {
  const nodesInitialized = useNodesInitialized();
  const { getNodes, getNodesBounds, setViewport } = useReactFlow();
  const width = useStore((state) => state.width);
  const height = useStore((state) => state.height);
  const currentZoom = useStore((state) => state.transform[2]);
  const lastLayoutKeyRef = useRef<string | null>(null);
  const lastZoomRef = useRef<number | null>(null);

  useEffect(() => {
    if (!nodesInitialized || width === 0 || height === 0) {
      return;
    }

    const layoutKey = `${graphKey}:${nodeCount}:${edgeCount}:${width}:${height}`;
    const isSameLayout = lastLayoutKeyRef.current === layoutKey;
    const isSameZoom =
      lastZoomRef.current != null && Math.abs(lastZoomRef.current - currentZoom) < 0.001;
    if (isSameLayout && isSameZoom) {
      return;
    }

    const nodes = getNodes();
    if (nodes.length === 0) {
      return;
    }

    const frame = requestAnimationFrame(() => {
      const bounds = getNodesBounds(nodes);
      const viewport = isSameLayout
        ? getTopCenterViewport(bounds, {
            width,
            height,
            minZoom: currentZoom,
            maxZoom: currentZoom,
          })
        : getTopCenterViewport(bounds, { width, height });
      void setViewport(viewport, { duration: 0 });
      lastLayoutKeyRef.current = layoutKey;
      lastZoomRef.current = viewport.zoom;
    });

    return () => cancelAnimationFrame(frame);
  }, [
    currentZoom,
    edgeCount,
    getNodes,
    getNodesBounds,
    graphKey,
    height,
    nodeCount,
    nodesInitialized,
    setViewport,
    width,
  ]);

  return null;
}

interface KnowledgeGraphViewProps {
  graph: KnowledgeGraphSnapshot;
  selectedId: string | null;
  onSelectNode: (id: string) => void;
}

// Position `count` items in a centered grid; returns (i) => {x, y}.
function gridPos(count: number, cols: number, xGap: number, yGap: number) {
  const c = Math.max(1, Math.min(cols, count || 1));
  return (i: number) => ({ x: (i % c) * xGap, y: Math.floor(i / c) * yGap });
}

function KnowledgeGraphViewInner({
  graph,
  selectedId,
  onSelectNode,
}: KnowledgeGraphViewProps) {
  const [focused, setFocused] = useState<string | null>(null);
  // Edge kinds the viewer has hidden (empty = show all). Kept as a hide-set so
  // it needs no resync when the graph — and thus its present kinds — changes.
  const [hiddenKinds, setHiddenKinds] = useState<Set<GraphEdgeKind>>(new Set());

  // The distinct edge kinds actually present, ordered by the legend for a
  // stable toggle row.
  const presentKinds = useMemo(() => {
    const present = new Set(graph.edges.map((edge) => edge.kind));
    return EDGE_LEGEND.map((entry) => entry.kind).filter((kind) =>
      present.has(kind),
    );
  }, [graph.edges]);

  function toggleKind(kind: GraphEdgeKind) {
    setHiddenKinds((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) {
        next.delete(kind);
      } else {
        next.add(kind);
      }
      return next;
    });
  }

  const { clusters, unclustered } = useMemo(() => groupByCluster(graph.nodes), [graph.nodes]);

  // Ordered list of groups (largest first), plus an "Unclustered" bucket.
  const groups = useMemo(() => {
    const list = [...clusters.entries()]
      .map(([id, entry]) => ({ key: String(id), label: entry.label || "Topic", members: entry.members }))
      .sort((a, b) => b.members.length - a.members.length);
    if (unclustered.length) {
      list.push({ key: UNCLUSTERED, label: "Unclustered", members: unclustered });
    }
    return list;
  }, [clusters, unclustered]);
  const hasClusters = clusters.size > 0;

  const focusedGroup = focused ? groups.find((g) => g.key === focused) ?? null : null;

  const { nodes, visibleCandidateIds } = useMemo(() => {
    const out: Node<CandidateNodeData | ClusterNodeData>[] = [];
    const visible = new Set<string>();

    // No cluster info at all → layered DAG when dependency edges exist, else
    // fall back to the plain grid.
    if (!hasClusters) {
      const positions =
        layoutDagNodes(graph.nodes, graph.edges) ?? layoutGraphNodes(graph.nodes);
      for (const node of graph.nodes) {
        visible.add(node.id);
        out.push({
          id: node.id,
          type: "candidateNode",
          position: positions.get(node.id) ?? { x: 0, y: 0 },
          selected: node.id === selectedId,
          data: {
            label: node.label,
            state: node.state,
            confidence: node.confidence,
            buildState: node.buildState,
          },
        });
      }
      return { nodes: out, visibleCandidateIds: visible };
    }

    // Drilled into one cluster → its member facts in a grid.
    if (focusedGroup) {
      const at = gridPos(focusedGroup.members.length, 5, 240, 150);
      focusedGroup.members.forEach((node, i) => {
        visible.add(node.id);
        out.push({
          id: node.id,
          type: "candidateNode",
          position: at(i),
          selected: node.id === selectedId,
          data: {
            label: node.label,
            state: node.state,
            confidence: node.confidence,
            buildState: node.buildState,
          },
        });
      });
      return { nodes: out, visibleCandidateIds: visible };
    }

    // Overview → super-nodes in a grid.
    const at = gridPos(groups.length, 4, 250, 130);
    groups.forEach((group, i) => {
      out.push({
        id: `group:${group.key}`,
        type: "clusterNode",
        position: at(i),
        selected: false,
        data: { label: group.label, count: group.members.length, expanded: false },
      });
    });
    return { nodes: out, visibleCandidateIds: visible };
  }, [graph.nodes, graph.edges, groups, focusedGroup, hasClusters, selectedId]);

  const edges: Edge[] = useMemo(
    () =>
      graph.edges
        .filter(
          (edge) =>
            !hiddenKinds.has(edge.kind) &&
            visibleCandidateIds.has(edge.src) &&
            visibleCandidateIds.has(edge.dst),
        )
        .map((edge, index) => ({
          id: `${edge.kind}-${edge.src}-${edge.dst}-${index}`,
          source: edge.src,
          target: edge.dst,
          className:
            edge.kind === "contradiction"
              ? "graph-edge graph-edge--contradiction"
              : edge.kind === "support"
                ? "graph-edge graph-edge--support"
                : edge.kind === "renders"
                  ? "graph-edge graph-edge--renders"
                  : edge.kind === "depends"
                    ? "graph-edge graph-edge--depends"
                    : "graph-edge graph-edge--similarity",
          animated: edge.kind === "contradiction",
          // depends_on is directional (prerequisite -> dependent); arrow it in
          // the accent color so it reads apart from gray similarity edges.
          markerEnd:
            edge.kind === "depends"
              ? { type: MarkerType.ArrowClosed, color: "#2563eb" }
              : undefined,
        })),
    [graph.edges, visibleCandidateIds, hiddenKinds],
  );

  function handleNodeClick(id: string) {
    if (id.startsWith("group:")) {
      setFocused(id.slice("group:".length));
      return;
    }
    onSelectNode(id);
  }

  if (graph.nodes.length === 0) {
    return <p className="muted">No graph nodes to display.</p>;
  }

  return (
    <div className="knowledge-graph-view">
      {focusedGroup ? (
        <button type="button" className="graph-back-btn" onClick={() => setFocused(null)}>
          ← All topics ({groups.length}) · <strong>{focusedGroup.label}</strong> ({focusedGroup.members.length})
        </button>
      ) : null}
      <ReactFlow
        key={focused ?? "overview"}
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        minZoom={0.2}
        maxZoom={1.5}
        onNodeClick={(_, node) => handleNodeClick(node.id)}
        proOptions={{ hideAttribution: true }}
      >
        <AlignGraphViewTopCenter
          graphKey={`${graph.source}:${focused ?? "overview"}:${nodes.length}`}
          nodeCount={nodes.length}
          edgeCount={edges.length}
        />
        <Background gap={16} size={1} />
        <Controls showInteractive={false} position="top-right" />
      </ReactFlow>
      <GraphLegend
        className="graph-legend--overlay"
        edgeKinds={presentKinds}
        hiddenKinds={hiddenKinds}
        onToggleKind={toggleKind}
      />
    </div>
  );
}

export const KnowledgeGraphView = memo(KnowledgeGraphViewInner);
