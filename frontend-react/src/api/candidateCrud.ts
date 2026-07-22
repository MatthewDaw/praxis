import { deriveGraphFromCandidates, cloneGraphSnapshot } from "./graphModel";
import { canDeleteCandidate, candidateStateLabel } from "./candidateModel";
import { buildPromoteBody, buildResolveBody } from "./contract";
import type { DataProvider } from "./dataProvider";
import type { Candidate } from "../types/candidate";
import type { KnowledgeGraphSnapshot } from "../types/graph";
import type { ParsedLogSession } from "../types/transcript";

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
    contradictions: [],
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

export interface InMemoryProviderOptions {
  getTranscript: () => ParsedLogSession | null;
  /** Trailing phrase for "…is not supported <suffix>" errors, e.g. "in mock mode". */
  unsupportedSuffix: string;
}

/**
 * The shared in-memory DataProvider used by both the mock (fixture) and
 * local-logs (heuristic) sources. Owns candidate/graph mutation (promote,
 * reject, CRUD, contradiction resolution) against seeded state; callers supply
 * the seed candidates/graph and the two source-specific bits (transcript and
 * the "not supported" error suffix).
 */
export function createInMemoryDataProvider(
  initialCandidates: Candidate[],
  initialGraph: KnowledgeGraphSnapshot,
  { getTranscript, unsupportedSuffix }: InMemoryProviderOptions,
): DataProvider {
  let candidates = initialCandidates;
  let graph = initialGraph;

  const syncGraphNodeState = (id: string, state: Candidate["state"]): void => {
    const node = graph.nodes.find((n) => n.id === id);
    if (node) {
      node.state = state;
    }
  };

  return {
    async listCandidates(state) {
      return state ? candidates.filter((c) => c.state === state) : [...candidates];
    },

    async getCandidate(id) {
      return candidates.find((c) => c.id === id) ?? null;
    },

    async promote(id) {
      const index = candidates.findIndex((c) => c.id === id);
      if (index < 0) {
        throw new Error(`Unknown candidate id: ${id}`);
      }
      const current = candidates[index];
      const body = buildPromoteBody(current.state);
      const updated: Candidate = {
        ...current,
        state: body.targetState as Candidate["state"],
        displayState: candidateStateLabel(body.targetState as Candidate["state"]),
        auditTrail: [
          ...current.auditTrail,
          {
            action: `promoted_to_${body.targetState}`,
            timestamp: new Date().toISOString(),
            provenance: current.provenance,
            actor: "human-gate",
          },
        ],
      };
      candidates[index] = updated;
      syncGraphNodeState(id, updated.state);
      return updated;
    },

    async reject(id, reason) {
      const index = candidates.findIndex((c) => c.id === id);
      if (index < 0) {
        throw new Error(`Unknown candidate id: ${id}`);
      }
      const current = candidates[index];
      candidates[index] = {
        ...current,
        state: "rejected",
        displayState: candidateStateLabel("rejected"),
        auditTrail: [
          ...current.auditTrail,
          {
            action: "rejected",
            timestamp: new Date().toISOString(),
            provenance: current.provenance,
            actor: "human-gate",
            note: reason,
          },
        ],
      };
      syncGraphNodeState(id, "rejected");
    },

    async createCandidate(input: CandidateWriteInput) {
      const created = buildNewCandidate(input);
      candidates = [...candidates, created];
      graph = refreshGraphFromCandidates(graph, candidates);
      return created;
    },

    async updateCandidate(id, input) {
      const index = candidates.findIndex((c) => c.id === id);
      if (index < 0) {
        throw new Error(`Unknown candidate id: ${id}`);
      }
      const updated = applyCandidateUpdate(candidates[index], input);
      candidates[index] = updated;
      graph = refreshGraphFromCandidates(graph, candidates);
      return updated;
    },

    async deleteCandidate(id) {
      const index = candidates.findIndex((c) => c.id === id);
      if (index < 0) {
        throw new Error(`Unknown candidate id: ${id}`);
      }
      if (!canDeleteCandidate(candidates[index])) {
        throw new Error("Reject this fact before deleting it.");
      }
      candidates = candidates
        .filter((c) => c.id !== id)
        .map((candidate) => ({
          ...candidate,
          contradictionIds: candidate.contradictionIds.filter((cid) => cid !== id),
        }));
      graph = refreshGraphFromCandidates(graph, candidates);
    },

    async resolveContradiction(contradictionId, resolution, keepId) {
      buildResolveBody(resolution, keepId);
      const kept = candidates.find((c) => c.id === keepId);
      if (!kept) {
        throw new Error(`Unknown keep id: ${keepId}`);
      }
      const [primaryId, rivalId] = contradictionId.split("__");
      candidates = candidates.map((candidate) => {
        if (candidate.id === primaryId || candidate.id === rivalId) {
          const otherId = candidate.id === primaryId ? rivalId : primaryId;
          const contradictionIds = Array.from(
            new Set([...candidate.contradictionIds, otherId]),
          );
          if (candidate.id !== keepId) {
            return {
              ...candidate,
              state: "rejected",
              displayState: candidateStateLabel("rejected"),
              contradictionIds,
            };
          }
          return { ...candidate, contradictionIds };
        }
        return candidate;
      });
      const loserId = keepId === primaryId ? rivalId : primaryId;
      syncGraphNodeState(loserId, "rejected");
      syncGraphNodeState(keepId, kept.state);
      return candidates.find((c) => c.id === keepId) ?? kept;
    },

    async getGraph() {
      return cloneGraphSnapshot(graph);
    },

    async clearGraph() {
      throw new Error(`Clearing the graph is not supported ${unsupportedSuffix}`);
    },

    async getTranscript() {
      return getTranscript();
    },

    async listSnapshots() {
      return [];
    },

    async saveSnapshot() {
      throw new Error(`Snapshots are not supported ${unsupportedSuffix}`);
    },

    async loadSnapshot() {
      throw new Error(`Snapshots are not supported ${unsupportedSuffix}`);
    },

    async deleteSnapshot() {
      throw new Error(`Snapshots are not supported ${unsupportedSuffix}`);
    },
  };
}
