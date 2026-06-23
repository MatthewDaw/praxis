import {
  canDeleteCandidate,
  candidateFromMapping,
  candidateStateLabel,
} from "./candidateModel";
import { distillCandidatesFromTranscript } from "./heuristicDistiller";
import {
  cloneGraphSnapshot,
  deriveGraphFromCandidates,
} from "./graphModel";
import { parseJsonlFiles } from "./jsonlParser";
import { buildPromoteBody, buildResolveBody } from "./contract";
import {
  applyCandidateUpdate,
  buildNewCandidate,
  refreshGraphFromCandidates,
  type CandidateWriteInput,
} from "./candidateCrud";
import type { DataProvider } from "./dataProvider";
import type { Candidate } from "../types/candidate";
import type { KnowledgeGraphSnapshot } from "../types/graph";
import type { LocalLogFileInput, ParsedLogSession } from "../types/transcript";

function syncGraphNodeState(
  graph: KnowledgeGraphSnapshot,
  id: string,
  state: Candidate["state"],
): void {
  const node = graph.nodes.find((n) => n.id === id);
  if (node) {
    node.state = state;
  }
}

export function buildLocalLogSession(files: LocalLogFileInput[]): ParsedLogSession {
  return parseJsonlFiles(files);
}

export function createLocalLogsDataProvider(session: ParsedLogSession): DataProvider {
  const rawCandidates = distillCandidatesFromTranscript(session.lines);
  let candidates = rawCandidates.map(candidateFromMapping);
  let graph = cloneGraphSnapshot(deriveGraphFromCandidates(candidates));
  graph.source = "derived";

  return {
    async listCandidates(state) {
      if (!state) {
        return [...candidates];
      }
      return candidates.filter((c) => c.state === state);
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
      syncGraphNodeState(graph, id, updated.state);
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
        state: "decayed",
        displayState: candidateStateLabel("decayed"),
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
      syncGraphNodeState(graph, id, "decayed");
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
              state: "decayed",
              displayState: candidateStateLabel("decayed"),
              contradictionIds,
            };
          }
          return {
            ...candidate,
            contradictionIds,
          };
        }
        return candidate;
      });
      const loserId = keepId === primaryId ? rivalId : primaryId;
      syncGraphNodeState(graph, loserId, "decayed");
      syncGraphNodeState(graph, keepId, kept.state);
      const updated = candidates.find((c) => c.id === keepId);
      return updated ?? kept;
    },

    async getGraph() {
      return cloneGraphSnapshot(graph);
    },

    async getTranscript() {
      return session;
    },

    async listSnapshots() {
      return [];
    },

    async saveSnapshot() {
      throw new Error("Snapshots are not supported for local logs");
    },

    async loadSnapshot(_name: string, _mode?: "add" | "replace") {
      throw new Error("Snapshots are not supported for local logs");
    },

    async deleteSnapshot() {
      throw new Error("Snapshots are not supported for local logs");
    },
  };
}

export function createEmptyLocalLogsProvider(): DataProvider {
  return {
    async listCandidates() {
      return [];
    },

    async getCandidate() {
      return null;
    },

    async promote(id) {
      throw new Error(`Unknown candidate id: ${id}`);
    },

    async reject(id) {
      throw new Error(`Unknown candidate id: ${id}`);
    },

    async createCandidate(input) {
      const created = buildNewCandidate(input);
      return created;
    },

    async updateCandidate(id) {
      throw new Error(`Unknown candidate id: ${id}`);
    },

    async deleteCandidate(id) {
      throw new Error(`Unknown candidate id: ${id}`);
    },

    async resolveContradiction() {
      throw new Error("No contradictions in empty local session");
    },

    async getGraph() {
      return {
        nodes: [],
        edges: [],
        source: "derived",
      };
    },

    async getTranscript() {
      return null;
    },

    async listSnapshots() {
      return [];
    },

    async saveSnapshot() {
      throw new Error("Snapshots are not supported for local logs");
    },

    async loadSnapshot(_name: string, _mode?: "add" | "replace") {
      throw new Error("Snapshots are not supported for local logs");
    },

    async deleteSnapshot() {
      throw new Error("Snapshots are not supported for local logs");
    },
  };
}

export function createLocalLogsDataProviderFromFiles(
  files: LocalLogFileInput[],
): DataProvider {
  const session = buildLocalLogSession(files);
  return createLocalLogsDataProvider(session);
}
