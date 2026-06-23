import type { CandidateState } from "./candidate";

export type GraphEdgeKind = "contradiction" | "support" | "similarity";

export interface GraphNode {
  id: string;
  label: string;
  state: CandidateState;
  confidence: number;
  scope?: string;
  category?: string;
  provenance?: string;
  clusterId?: number | null;
  clusterLabel?: string;
}

export interface GraphEdge {
  src: string;
  dst: string;
  kind: GraphEdgeKind;
}

export interface ScopeGroup {
  id: string;
  label: string;
  parentId: string | null;
  memberIds: string[];
}

export type GraphSnapshotSource = "mock" | "api" | "derived";

export interface KnowledgeGraphSnapshot {
  nodes: GraphNode[];
  edges: GraphEdge[];
  scopeGroups?: ScopeGroup[];
  source: GraphSnapshotSource;
}
