import { candidateFromMapping } from "./candidateModel";
import { distillCandidatesFromTranscript } from "./heuristicDistiller";
import {
  cloneGraphSnapshot,
  deriveGraphFromCandidates,
} from "./graphModel";
import { parseJsonlFiles } from "./jsonlParser";
import {
  buildNewCandidate,
  createInMemoryDataProvider,
} from "./candidateCrud";
import type { DataProvider } from "./dataProvider";
import type { LocalLogFileInput, ParsedLogSession } from "../types/transcript";

export function buildLocalLogSession(files: LocalLogFileInput[]): ParsedLogSession {
  return parseJsonlFiles(files);
}

export function createLocalLogsDataProvider(session: ParsedLogSession): DataProvider {
  const rawCandidates = distillCandidatesFromTranscript(session.lines);
  const candidates = rawCandidates.map(candidateFromMapping);
  const graph = cloneGraphSnapshot(deriveGraphFromCandidates(candidates));
  graph.source = "derived";

  return createInMemoryDataProvider(candidates, graph, {
    getTranscript: () => session,
    unsupportedSuffix: "for local logs",
  });
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

    async clearGraph() {
      throw new Error("Clearing the graph is not supported for local logs");
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
