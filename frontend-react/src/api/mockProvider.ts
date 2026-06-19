import { createApiDataProvider } from "./apiClient";
import { candidateFromMapping, parseCandidateList } from "./candidateModel";
import { buildPromoteBody, buildResolveBody } from "./contract";
import type { DataProvider } from "./dataProvider";
import type { Candidate, EvalMetrics } from "../types/candidate";

const PLACEHOLDER_METRICS: EvalMetrics = {
  source: "placeholder",
  correctionRate: [1.0, 0.72, 0.48, 0.35],
  sessions: ["cold", "run_1", "run_2", "run_3"],
  correctionsBefore: 12,
  correctionsAfter: 5,
};

export function createMockDataProvider(): DataProvider {
  let candidates: Candidate[] = [];

  async function ensureLoaded(): Promise<void> {
    if (candidates.length > 0) {
      return;
    }
    const response = await fetch("/mock-candidates.json");
    const payload = await response.json();
    candidates = parseCandidateList(payload).map(candidateFromMapping);
  }

  return {
    async listCandidates(state) {
      await ensureLoaded();
      if (!state) {
        return [...candidates];
      }
      return candidates.filter((c) => c.displayState === state);
    },

    async getCandidate(id) {
      await ensureLoaded();
      return candidates.find((c) => c.id === id) ?? null;
    },

    async promote(id) {
      await ensureLoaded();
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
      await ensureLoaded();
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
      await ensureLoaded();
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
      return kept;
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
