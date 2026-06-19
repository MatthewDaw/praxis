import { createApiDataProvider } from "./apiClient";
import { candidateFromMapping, parseCandidateList } from "./candidateModel";
import { buildPromoteBody, buildResolveBody } from "./contract";
import type { DataProvider } from "./dataProvider";
import type { Candidate, EvalMetrics, RawCandidate } from "../types/candidate";

const PLACEHOLDER_METRICS: EvalMetrics = {
  source: "placeholder",
  correctionRate: [1.0, 0.72, 0.48, 0.35],
  sessions: ["cold", "run_1", "run_2", "run_3"],
  correctionsBefore: 12,
  correctionsAfter: 5,
};

export function createMockDataProviderWithRows(rows: RawCandidate[]): DataProvider {
  let candidates = rows.map(candidateFromMapping);

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
      const updated = candidates.find((c) => c.id === keepId);
      return updated ?? kept;
    },

    async getEvalMetrics() {
      return PLACEHOLDER_METRICS;
    },
  };
}

export function createMockDataProvider(): DataProvider {
  let delegate: DataProvider | null = null;

  async function load(): Promise<DataProvider> {
    if (!delegate) {
      const response = await fetch("/mock-candidates.json");
      const payload = await response.json();
      delegate = createMockDataProviderWithRows(parseCandidateList(payload));
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
      const url = import.meta.env.VITE_PRAXIS_EVAL_METRICS_URL?.trim();
      if (url) {
        try {
          const response = await fetch(url);
          if (response.ok) {
            const payload = (await response.json()) as Record<string, unknown>;
            return {
              source: url,
              correctionRate:
                (payload.correction_rate as number[]) ??
                (payload.correctionRate as number[]) ??
                PLACEHOLDER_METRICS.correctionRate,
              sessions: payload.sessions as string[] | undefined,
              correctionsBefore:
                (payload.corrections_before as number | undefined) ??
                (payload.correctionsBefore as number | undefined),
              correctionsAfter:
                (payload.corrections_after as number | undefined) ??
                (payload.correctionsAfter as number | undefined),
            };
          }
        } catch {
          /* fall through to placeholder */
        }
      }
      return PLACEHOLDER_METRICS;
    },
  };
}

export function getDataProvider(): DataProvider {
  const baseUrl = import.meta.env.VITE_PRAXIS_API_BASE_URL?.trim();
  if (baseUrl) {
    const token = import.meta.env.VITE_PRAXIS_API_TOKEN?.trim();
    return createApiDataProvider(baseUrl, token || undefined);
  }
  return createMockDataProvider();
}
