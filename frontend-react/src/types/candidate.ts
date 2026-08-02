export type CandidateState =
  | "proposed"
  | "active"
  | "rejected"
  | "unrecognized";

export interface ConfidenceBreakdown {
  frequency: number;
  recency: number;
  breadth: number;
  frequencyRationale?: string;
  recencyRationale?: string;
  breadthRationale?: string;
}

/**
 * One provenance event on a fact. Entry shapes are heterogeneous in real data — a
 * `human-gate` edit, an `af-build/<project>` edit, and a `compacted` marker carrying a
 * `note` all coexist on the same trail — so the known fields are optional-by-default
 * (normalized to "") and anything else the writer recorded is preserved under `extra`
 * rather than dropped on the floor.
 */
export interface AuditEntry {
  action: string;
  timestamp: string;
  provenance: string;
  actor: string;
  note?: string;
  extra?: Record<string, unknown>;
}

export type ContradictionStatus = "pending" | "resolved";

export interface ContradictionLink {
  id: string;
  status: ContradictionStatus;
}

export interface CandidateWriteInput {
  title: string;
  content: string;
  provenance?: string;
  confidence?: number;
}

export interface Candidate {
  id: string;
  title: string;
  content: string;
  state: CandidateState;
  displayState: string;
  confidence: number;
  provenance: string;
  createdAt: string;
  confidenceBreakdown?: ConfidenceBreakdown;
  contradictionIds: string[];
  contradictions: ContradictionLink[];
  auditTrail: AuditEntry[];
  extra: Record<string, unknown>;
}

export interface ApiConflictError extends Error {
  statusCode: 409;
  candidateId?: string;
}

export interface ApiClientError extends Error {
  statusCode: number;
}

export type RawCandidate = Record<string, unknown>;
