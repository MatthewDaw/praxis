import { deriveGraphFromCandidates, cloneGraphSnapshot } from "./graphModel";
import { candidateStateLabel } from "./candidateModel";
import type { Candidate } from "../types/candidate";
import type { KnowledgeGraphSnapshot } from "../types/graph";

export interface CandidateWriteInput {
  title: string;
  content: string;
  provenance?: string;
  confidence?: number;
}

export function buildNewCandidate(input: CandidateWriteInput, id?: string): Candidate {
  const now = new Date().toISOString();
  const provenance = input.provenance?.trim() || `human-gate/manual:${now}`;

  return {
    id: id ?? `cand_${Date.now()}`,
    title: input.title.trim(),
    content: input.content.trim(),
    state: "proposed",
    displayState: candidateStateLabel("proposed"),
    confidence: clampConfidence(input.confidence ?? 0.5),
    provenance,
    createdAt: now,
    contradictionIds: [],
    auditTrail: [
      {
        action: "created",
        timestamp: now,
        provenance,
        actor: "human-gate",
      },
    ],
    extra: {},
  };
}

export function applyCandidateUpdate(
  current: Candidate,
  input: CandidateWriteInput,
): Candidate {
  const now = new Date().toISOString();
  const provenance = input.provenance?.trim() || current.provenance;

  return {
    ...current,
    title: input.title.trim(),
    content: input.content.trim(),
    provenance,
    confidence:
      input.confidence != null ? clampConfidence(input.confidence) : current.confidence,
    auditTrail: [
      ...current.auditTrail,
      {
        action: "edited",
        timestamp: now,
        provenance: current.provenance,
        actor: "human-gate",
      },
    ],
  };
}

export function refreshGraphFromCandidates(
  graph: KnowledgeGraphSnapshot,
  candidates: Candidate[],
): KnowledgeGraphSnapshot {
  const derived = deriveGraphFromCandidates(candidates);
  return {
    ...cloneGraphSnapshot(derived),
    scopeGroups: graph.scopeGroups,
    source: graph.source,
  };
}

function clampConfidence(value: number): number {
  if (Number.isNaN(value)) {
    return 0.5;
  }
  return Math.min(1, Math.max(0, value));
}
