import { candidateFromMapping, parseCandidateList } from "./candidateModel";
import {
  cloneGraphSnapshot,
  deriveGraphFromCandidates,
  parseGraphPayload,
} from "./graphModel";
import { buildPromoteBody, buildResolveBody } from "./contract";
import type { DataProvider } from "./dataProvider";
import type { Candidate, EvalMetrics, RawCandidate } from "../types/candidate";
import type {
  KnowledgeGraphSnapshot,
} from "../types/graph";

const PLACEHOLDER_METRICS: EvalMetrics = {
  source: "placeholder",
  correctionRate: [1.0, 0.72, 0.48, 0.35],
  sessions: ["cold", "run_1", "run_2", "run_3"],
  correctionsBefore: 12,
  correctionsAfter: 5,
};

const MOCK_EVAL_METRICS_URL = "/mock-eval-metrics.json";

function metricsFromPayload(
  payload: Record<string, unknown>,
  source: string,
): EvalMetrics {
  return {
    source,
    correctionRate:
      (payload.correction_rate as number[]) ??
      (payload.correctionRate as number[]) ??
      PLACEHOLDER_METRICS.correctionRate,
    sessions:
      (payload.sessions as string[] | undefined) ??
      PLACEHOLDER_METRICS.sessions,
    correctionsBefore:
      (payload.corrections_before as number | undefined) ??
      (payload.correctionsBefore as number | undefined) ??
      PLACEHOLDER_METRICS.correctionsBefore,
    correctionsAfter:
      (payload.corrections_after as number | undefined) ??
      (payload.correctionsAfter as number | undefined) ??
      PLACEHOLDER_METRICS.correctionsAfter,
  };
}

function placeholderMetrics(fetchError?: string): EvalMetrics {
  return fetchError
    ? { ...PLACEHOLDER_METRICS, fetchError }
    : PLACEHOLDER_METRICS;
}

let mockEvalMetricsPromise: Promise<EvalMetrics> | null = null;

function fetchMockEvalMetrics(): Promise<EvalMetrics> {
  if (!mockEvalMetricsPromise) {
    mockEvalMetricsPromise = fetch(MOCK_EVAL_METRICS_URL)
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(response.statusText);
        }
        const payload = (await response.json()) as Record<string, unknown>;
        return metricsFromPayload(payload, "mock");
      })
      .catch((error: unknown) =>
        placeholderMetrics(
          error instanceof Error ? error.message : "Mock eval metrics unavailable",
        ),
      );
  }
  return mockEvalMetricsPromise;
}

function fetchEvalMetrics(
  url: string | undefined,
  token: string | undefined,
): Promise<EvalMetrics> {
  if (!url) {
    return fetchMockEvalMetrics();
  }
  return fetch(url, {
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(response.statusText);
      }
      const payload = (await response.json()) as Record<string, unknown>;
      return metricsFromPayload(payload, url);
    })
    .catch((error: unknown) =>
      placeholderMetrics(
        error instanceof Error ? error.message : "Eval metrics unavailable",
      ),
    );
}

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

function removeContradictionEdges(
  graph: KnowledgeGraphSnapshot,
  idA: string,
  idB: string,
): void {
  graph.edges = graph.edges.filter(
    (edge) =>
      edge.kind !== "contradiction" ||
      !(
        (edge.src === idA && edge.dst === idB) ||
        (edge.src === idB && edge.dst === idA)
      ),
  );
}

export function createMockDataProviderWithRows(
  rows: RawCandidate[],
  graphSnapshot?: KnowledgeGraphSnapshot,
  evalMetricsUrl?: string,
  apiToken?: string,
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
      return candidates.filter((c) => c.displayState === state);
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
        displayState: body.targetState,
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
        displayState: "decayed",
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

    async resolveContradiction(contradictionId, resolution, keepId) {
      buildResolveBody(resolution, keepId);
      const kept = candidates.find((c) => c.id === keepId);
      if (!kept) {
        throw new Error(`Unknown keep id: ${keepId}`);
      }
      const [primaryId, rivalId] = contradictionId.split("__");
      candidates = candidates.map((candidate) => {
        if (candidate.id === primaryId || candidate.id === rivalId) {
          if (candidate.id !== keepId) {
            return {
              ...candidate,
              state: "decayed",
              displayState: "decayed",
              contradictionIds: candidate.contradictionIds.filter(
                (cid) => cid !== primaryId && cid !== rivalId,
              ),
            };
          }
          return {
            ...candidate,
            contradictionIds: candidate.contradictionIds.filter(
              (cid) => cid !== primaryId && cid !== rivalId,
            ),
          };
        }
        return candidate;
      });
      removeContradictionEdges(graph, primaryId, rivalId);
      const loserId = keepId === primaryId ? rivalId : primaryId;
      syncGraphNodeState(graph, loserId, "decayed");
      syncGraphNodeState(graph, keepId, kept.state);
      const updated = candidates.find((c) => c.id === keepId);
      return updated ?? kept;
    },

    async getEvalMetrics() {
      return fetchEvalMetrics(evalMetricsUrl, apiToken);
    },

    async getGraph() {
      return cloneGraphSnapshot(graph);
    },

    async getTranscript() {
      return null;
    },
  };
}

export function createMockDataProvider(
  evalMetricsUrl?: string,
  apiToken?: string,
): DataProvider {
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
        evalMetricsUrl,
        apiToken,
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

    async resolveContradiction(contradictionId, resolution, keepId) {
      return (await load()).resolveContradiction(
        contradictionId,
        resolution,
        keepId,
      );
    },

    async getEvalMetrics() {
      return fetchEvalMetrics(evalMetricsUrl, apiToken);
    },

    async getGraph() {
      return (await load()).getGraph();
    },

    async getTranscript() {
      return null;
    },
  };
}
