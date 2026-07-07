import type { CandidateState } from "./candidate";

export type GraphEdgeKind =
  | "contradiction"
  | "support"
  | "similarity"
  | "renders"
  | "depends";

export type TicketBuildState =
  | "incomplete"
  | "in_progress"
  | "finished"
  | "blocked";

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
  /** True for ticket nodes (requirement facts). */
  isTicket?: boolean;
  /** Build-loop lifecycle state — present only on ticket nodes. */
  buildState?: TicketBuildState;
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
