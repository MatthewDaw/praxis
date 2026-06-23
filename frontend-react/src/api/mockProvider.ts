import {
  canDeleteCandidate,
  candidateFromMapping,
  candidateStateLabel,
  parseCandidateList,
} from "./candidateModel";
import {
  cloneGraphSnapshot,
  deriveGraphFromCandidates,
  parseGraphPayload,
} from "./graphModel";
import { buildPromoteBody, buildResolveBody } from "./contract";
import {
  applyCandidateUpdate,
  buildNewCandidate,
  refreshGraphFromCandidates,
  type CandidateWriteInput,
} from "./candidateCrud";
import type { DataProvider } from "./dataProvider";
import type { Candidate, RawCandidate } from "../types/candidate";
import type {
  KnowledgeGraphSnapshot,
} from "../types/graph";

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

export function createMockDataProviderWithRows(
  rows: RawCandidate[],
  graphSnapshot?: KnowledgeGraphSnapshot,
): DataProvider {
  let candidates = rows.map(candidateFromMapping);
  let graph = graphSnapshot
    ? cloneGraphSnapshot({ ...graphSnapshot, source: "mock" })
    : deriveGraphFromCandidates(candidates);

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
      return null;
    },

    async listSnapshots() {
      return [];
    },

    async saveSnapshot() {
      throw new Error("Snapshots are not supported in mock mode");
    },

    async loadSnapshot(_name: string, _mode?: "add" | "replace") {
      throw new Error("Snapshots are not supported in mock mode");
    },

    async deleteSnapshot() {
      throw new Error("Snapshots are not supported in mock mode");
    },
  };
}

export function createMockDataProvider(): DataProvider {
  let delegate: DataProvider | null = null;

  async function load(): Promise<DataProvider> {
    if (!delegate) {
      const [candidatesResponse, graphResponse] = await Promise.all([
        fetch("/mock-candidates.json"),
        fetch("/mock-graph.json"),
      ]);
      const candidatesPayload = await candidatesResponse.json();
      let graphSnapshot: KnowledgeGraphSnapshot = {
        nodes: [],
        edges: [],
        source: "mock",
      };
      if (graphResponse.ok) {
        const graphPayload = await graphResponse.json();
        graphSnapshot = parseGraphPayload(graphPayload, "mock");
      }
      delegate = createMockDataProviderWithRows(
        parseCandidateList(candidatesPayload),
        graphSnapshot,
      );
    }
    return delegate;
  }

  return {
    async listCandidates(state) {
      return (await load()).listCandidates(state);
    },

    async getCandidate(id) {
      return (await load()).getCandidate(id);
    },

    async promote(id) {
      return (await load()).promote(id);
    },

    async reject(id, reason) {
      await (await load()).reject(id, reason);
    },

    async createCandidate(input) {
      return (await load()).createCandidate(input);
    },

    async updateCandidate(id, input) {
      return (await load()).updateCandidate(id, input);
    },

    async deleteCandidate(id) {
      await (await load()).deleteCandidate(id);
    },

    async resolveContradiction(contradictionId, resolution, keepId) {
      return (await load()).resolveContradiction(
        contradictionId,
        resolution,
        keepId,
      );
    },

    async getGraph() {
      return (await load()).getGraph();
    },

    async getTranscript() {
      return null;
    },

    async listSnapshots() {
      return (await load()).listSnapshots();
    },

    async saveSnapshot(name) {
      return (await load()).saveSnapshot(name);
    },

    async loadSnapshot(name, mode) {
      return (await load()).loadSnapshot(name, mode);
    },

    async deleteSnapshot(name) {
      return (await load()).deleteSnapshot(name);
    },
  };
}
