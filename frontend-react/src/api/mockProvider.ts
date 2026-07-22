import { candidateFromMapping, parseCandidateList } from "./candidateModel";
import {
  cloneGraphSnapshot,
  deriveGraphFromCandidates,
  parseGraphPayload,
} from "./graphModel";
import { createInMemoryDataProvider } from "./candidateCrud";
import type { DataProvider } from "./dataProvider";
import type { RawCandidate } from "../types/candidate";
import type { KnowledgeGraphSnapshot } from "../types/graph";

export function createMockDataProviderWithRows(
  rows: RawCandidate[],
  graphSnapshot?: KnowledgeGraphSnapshot,
): DataProvider {
  const candidates = rows.map(candidateFromMapping);
  const graph = graphSnapshot
    ? cloneGraphSnapshot({ ...graphSnapshot, source: "mock" })
    : deriveGraphFromCandidates(candidates);

  return createInMemoryDataProvider(candidates, graph, {
    getTranscript: () => null,
    unsupportedSuffix: "in mock mode",
  });
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

    async clearGraph() {
      return (await load()).clearGraph();
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
