/**
 * @file candidate.ts
 * @usage Core TypeScript data contracts for PRAXIS knowledge candidates. Shared across dashboard, pipeline, and eval layers. Import for any component or service handling review items. Defines human-gate states, provenance for audit trails, and multi-factor confidence scoring.
 * @example import type { Candidate, GateState } from './types/candidate'; const c: Candidate = { ... };
 * @author Monica Peters <monigarr@MoniGarr.com>
 * @created 2026-06-17
 */

export type GateState = 'proposed' | 'suggested' | 'active';

export interface Provenance {
  /** Absolute or relative path to originating JSONL log file */
  sourceLogPath: string;
  /** Line number/offset in the log for exact traceability */
  lineOffset: number;
}

export interface ConfidenceScore {
  /** Frequency: how often this pattern appeared across sessions (0-1 normalized) */
  frequency: number;
  /** Recency: exponential decay based on last occurrence timestamp (0-1) */
  recency: number;
  /** Breadth: diversity of task contexts / files involved (0-1) */
  breadth: number;
  /** Optional rationale tooltip text explaining the aggregate */
  rationale?: string;
}

export interface Candidate {
  id: string;
  /** Human-readable title distilled from log */
  title: string;
  /** Full lesson / pattern description */
  content: string;
  state: GateState;
  confidence: ConfidenceScore;
  provenance: Provenance;
  /** Optional contradictions list for resolution UI */
  contradictions?: string[];
  createdAt: string; // ISO date
}

/**
 * Exhaustive type guard example for GateState (per shared rules)
 */
export function isGateState(value: string): value is GateState {
  return ['proposed', 'suggested', 'active'].includes(value as GateState);
}
