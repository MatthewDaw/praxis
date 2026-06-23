import { GraphSummary } from "./GraphSummary";
import { KnowledgeGraphView } from "./KnowledgeGraphView";
import { ScopeTree } from "./ScopeTree";
import { StateFunnel } from "./StateFunnel";
import type { Candidate } from "../../types/candidate";
import type { KnowledgeGraphSnapshot } from "../../types/graph";

interface GraphExplorerProps {
  graph: KnowledgeGraphSnapshot;
  candidates: Candidate[];
  selectedId: string | null;
  onSelectNode: (id: string) => void;
}

export function GraphExplorer({
  graph,
  candidates,
  selectedId,
  onSelectNode,
}: GraphExplorerProps) {
  return (
    <div className="graph-explorer">
      <div className="graph-explorer__header">
        <StateFunnel candidates={candidates} />
        <GraphSummary graph={graph} />
      </div>
      <div className="graph-explorer__body">
        <aside className="graph-explorer__sidebar">
          <ScopeTree
            scopeGroups={graph.scopeGroups}
            selectedId={selectedId}
            onSelectNode={onSelectNode}
          />
        </aside>
        <section className="graph-explorer__canvas" aria-label="Graph">
          <p className="graph-canvas__label">Graph</p>
          <KnowledgeGraphView
            graph={graph}
            selectedId={selectedId}
            onSelectNode={onSelectNode}
          />
        </section>
      </div>
    </div>
  );
}
