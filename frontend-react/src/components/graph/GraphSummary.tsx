import { useMemo } from "react";
import type { KnowledgeGraphSnapshot } from "../../types/graph";

interface GraphSummaryProps {
  graph: KnowledgeGraphSnapshot;
}

export function GraphSummary({ graph }: GraphSummaryProps) {
  const summary = useMemo(() => {
    let contradictionCount = 0;
    let supportCount = 0;
    let rendersCount = 0;
    for (const edge of graph.edges) {
      if (edge.kind === "contradiction") contradictionCount += 1;
      else if (edge.kind === "support") supportCount += 1;
      else if (edge.kind === "renders") rendersCount += 1;
    }
    const surfaceCount = graph.nodes.filter((n) => n.category === "surface").length;
    const edgeBreakdown = `${contradictionCount} contradictions, ${supportCount} support, ${rendersCount} renders`;
    return (
      `${graph.nodes.length} nodes, ${graph.edges.length} edges (${edgeBreakdown})` +
      (surfaceCount > 0 ? `; ${surfaceCount} surfaces` : "")
    );
  }, [graph]);

  return (
    <div className="graph-summary" role="img" aria-label={summary}>
      <span className="graph-summary__label">Graph snapshot</span>
      <p className="graph-summary__text">{summary}</p>
      <p className="muted graph-summary__source">Source: {graph.source}</p>
    </div>
  );
}
