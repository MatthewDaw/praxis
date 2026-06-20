import { CandidateDetail } from "../CandidateDetail";
import { GraphSummary } from "./GraphSummary";
import { KnowledgeGraphView } from "./KnowledgeGraphView";
import { ScopeTree } from "./ScopeTree";
import { StateFunnel } from "./StateFunnel";
import type { Candidate } from "../../types/candidate";
import type { DataSourceMode } from "../../config/dataSource";
import type { KnowledgeGraphSnapshot } from "../../types/graph";

interface GraphExplorerProps {
  graph: KnowledgeGraphSnapshot;
  candidates: Candidate[];
  filteredCandidates: Candidate[];
  selectedId: string | null;
  onSelectNode: (id: string) => void;
  onResolve: (
    contradictionId: string,
    resolution: "keep_primary" | "keep_rival",
    keepId: string,
    rivalTitle: string,
  ) => Promise<void>;
  onDefer: (primaryTitle: string, rivalTitle: string) => void;
  dataSourceMode?: DataSourceMode;
}

export function GraphExplorer({
  graph,
  candidates,
  filteredCandidates,
  selectedId,
  onSelectNode,
  onResolve,
  onDefer,
  dataSourceMode,
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
        <aside className="graph-explorer__detail">
          <CandidateDetail
            candidates={filteredCandidates}
            selectedId={selectedId}
            onSelect={onSelectNode}
            onResolve={onResolve}
            onDefer={onDefer}
            dataSourceMode={dataSourceMode}
          />
        </aside>
      </div>
    </div>
  );
}
