export type CandidateState =
  | "proposed"
  | "suggested"
  | "active"
  | "decayed"
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

export interface EvalMetrics {
  correctionRate: number[];
  sessions?: string[];
  correctionsBefore?: number;
  correctionsAfter?: number;
  source: string;
}

export interface ApiConflictError extends Error {
  statusCode: 409;
  candidateId?: string;
}

export interface ApiClientError extends Error {
  statusCode: number;
}

export type RawCandidate = Record<string, unknown>;
