import { memo, useEffect, useMemo, useRef } from "react";
import {
  Background,
  Controls,
  Handle,
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
import type { KnowledgeGraphSnapshot } from "../../types/graph";
import { GraphLegend } from "./GraphLegend";
import { getTopCenterViewport, layoutGraphNodes, stateNodeColors } from "./graphLayout";

interface CandidateNodeData extends Record<string, unknown> {
  label: string;
  state: CandidateState;
  confidence: number;
}

function CandidateGraphNode({ data, selected }: NodeProps<Node<CandidateNodeData>>) {
  const colors = stateNodeColors(data.state);
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
      <p className="graph-node__title">{data.label}</p>
      <p className="graph-node__meta">
        {data.state} · {Math.round(data.confidence * 100)}%
      </p>
      <Handle type="source" position={Position.Bottom} className="graph-node__handle" />
    </div>
  );
}

const nodeTypes = {
  candidateNode: CandidateGraphNode,
};

function FitGraphViewTopCenter({
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
  const lastFitKeyRef = useRef<string | null>(null);

  useEffect(() => {
    if (!nodesInitialized || width === 0 || height === 0) {
      return;
    }

    const fitKey = `${graphKey}:${nodeCount}:${edgeCount}:${width}:${height}`;
    if (lastFitKeyRef.current === fitKey) {
      return;
    }

    const nodes = getNodes();
    if (nodes.length === 0) {
      return;
    }

    const frame = requestAnimationFrame(() => {
      const bounds = getNodesBounds(nodes);
      const viewport = getTopCenterViewport(bounds, { width, height });
      void setViewport(viewport, { duration: 0 });
      lastFitKeyRef.current = fitKey;
    });

    return () => cancelAnimationFrame(frame);
  }, [
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

function KnowledgeGraphViewInner({
  graph,
  selectedId,
  onSelectNode,
}: KnowledgeGraphViewProps) {
  const positions = useMemo(
    () => layoutGraphNodes(graph.nodes),
    [graph.nodes],
  );

  const nodes: Node<CandidateNodeData>[] = useMemo(
    () =>
      graph.nodes.map((node) => ({
        id: node.id,
        type: "candidateNode",
        position: positions.get(node.id) ?? { x: 0, y: 0 },
        selected: node.id === selectedId,
        data: {
          label: node.label,
          state: node.state,
          confidence: node.confidence,
        },
      })),
    [graph.nodes, positions, selectedId],
  );

  const edges: Edge[] = useMemo(
    () =>
      graph.edges.map((edge, index) => ({
        id: `${edge.kind}-${edge.src}-${edge.dst}-${index}`,
        source: edge.src,
        target: edge.dst,
        className:
          edge.kind === "contradiction"
            ? "graph-edge graph-edge--contradiction"
            : edge.kind === "support"
              ? "graph-edge graph-edge--support"
              : "graph-edge graph-edge--similarity",
        animated: edge.kind === "contradiction",
      })),
    [graph.edges],
  );

  if (graph.nodes.length === 0) {
    return <p className="muted">No graph nodes to display.</p>;
  }

  return (
    <div className="knowledge-graph-view">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        minZoom={0.2}
        maxZoom={1.5}
        onNodeClick={(_, node) => onSelectNode(node.id)}
        proOptions={{ hideAttribution: true }}
      >
        <FitGraphViewTopCenter
          graphKey={`${graph.source}:${graph.nodes.length}:${graph.edges.length}`}
          nodeCount={graph.nodes.length}
          edgeCount={graph.edges.length}
        />
        <Background gap={16} size={1} />
        <Controls showInteractive={false} position="top-right" />
      </ReactFlow>
      <GraphLegend className="graph-legend--overlay" />
    </div>
  );
}

export const KnowledgeGraphView = memo(KnowledgeGraphViewInner);
