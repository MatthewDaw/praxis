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

export interface AuditEntry {
  action: string;
  timestamp: string;
  provenance: string;
  actor: string;
  note?: string;
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
