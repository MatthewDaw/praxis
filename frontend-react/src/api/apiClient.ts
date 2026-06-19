import {
  buildPromoteBody,
  buildPromoteBodyImplicit,
  buildRejectBody,
  buildResolveBody,
  contractHeaders,
} from "./contract";
import {
  candidateFromMapping,
  parseCandidateList,
} from "./candidateModel";
import type { DataProvider } from "./dataProvider";
import type { EvalMetrics } from "../types/candidate";

class ApiConflictError extends Error {
  readonly statusCode = 409 as const;
  candidateId?: string;

  constructor(message: string, candidateId?: string) {
    super(message);
    this.name = "ApiConflictError";
    this.candidateId = candidateId;
  }
}

class ApiClientError extends Error {
  statusCode: number;

  constructor(message: string, statusCode: number) {
    super(message);
    this.name = "ApiClientError";
    this.statusCode = statusCode;
  }
}

function extractCandidateId(path: string): string | undefined {
  const prefix = "/candidates/";
  if (!path.includes(prefix)) {
    return undefined;
  }
  const segment = path.split(prefix)[1]?.split("/")[0];
  return segment ? decodeURIComponent(segment) : undefined;
}

async function parseJsonResponse(response: Response): Promise<unknown> {
  const raw = await response.text();
  if (!raw.trim()) {
    return {};
  }
  return JSON.parse(raw) as unknown;
}

export function createApiDataProvider(
  baseUrl: string,
  token?: string,
): DataProvider {
  const root = baseUrl.replace(/\/$/, "");

  async function request(
    method: string,
    path: string,
    body?: Record<string, unknown>,
  ): Promise<unknown> {
    const response = await fetch(`${root}${path}`, {
      method,
      headers: contractHeaders(token),
      body: body ? JSON.stringify(body) : undefined,
    });

    if (!response.ok) {
      const detail = await response.text();
      if (response.status === 409) {
        throw new ApiConflictError(
          detail || response.statusText,
          extractCandidateId(path),
        );
      }
      throw new ApiClientError(
        `API ${method} ${path} failed (${response.status}): ${detail || response.statusText}`,
        response.status,
      );
    }

    return parseJsonResponse(response);
  }

  return {
    async listCandidates(state?: string) {
      const query = state ? `?state=${encodeURIComponent(state)}` : "";
      const payload = await request("GET", `/candidates${query}`);
      return parseCandidateList(payload).map(candidateFromMapping);
    },

    async getCandidate(id) {
      try {
        const payload = await request(
          "GET",
          `/candidates/${encodeURIComponent(id)}`,
        );
        if (payload && typeof payload === "object") {
          return candidateFromMapping(payload as Record<string, unknown>);
        }
        return null;
      } catch (error) {
        if (error instanceof ApiClientError && error.statusCode === 404) {
          return null;
        }
        throw error;
      }
    },

    async promote(id) {
      const current = await this.getCandidate(id);
      if (!current) {
        throw new Error(`Unknown candidate id: ${id}`);
      }

      const path = `/candidates/${encodeURIComponent(id)}/promote`;
      try {
        const payload = await request("POST", path, buildPromoteBody(current.state));
        return candidateFromMapping(payload as Record<string, unknown>);
      } catch (error) {
        if (
          error instanceof ApiClientError &&
          (error.statusCode === 400 || error.statusCode === 422)
        ) {
          const payload = await request("POST", path, buildPromoteBodyImplicit());
          return candidateFromMapping(payload as Record<string, unknown>);
        }
        throw error;
      }
    },

    async reject(id, reason) {
      await request(
        "POST",
        `/candidates/${encodeURIComponent(id)}/reject`,
        buildRejectBody(reason),
      );
    },

    async resolveContradiction(contradictionId, resolution, keepId) {
      const payload = await request(
        "POST",
        `/contradictions/${encodeURIComponent(contradictionId)}/resolve`,
        buildResolveBody(resolution, keepId),
      );
      return candidateFromMapping(payload as Record<string, unknown>);
    },

    async getEvalMetrics() {
      const url = import.meta.env.VITE_PRAXIS_EVAL_METRICS_URL?.trim();
      if (!url) {
        return {
          source: "placeholder",
          correctionRate: [1.0, 0.72, 0.48, 0.35],
        };
      }

      try {
        const response = await fetch(url, {
          headers: contractHeaders(token),
        });
        if (!response.ok) {
          throw new Error(response.statusText);
        }
        const payload = (await response.json()) as Record<string, unknown>;
        return normalizeEvalMetrics(payload, url);
      } catch {
        return {
          source: "placeholder",
          correctionRate: [1.0, 0.72, 0.48, 0.35],
        };
      }
    },
  };
}

function normalizeEvalMetrics(
  payload: Record<string, unknown>,
  source: string,
): EvalMetrics {
  const series =
    (payload.correction_rate as number[] | undefined) ??
    (payload.correctionRate as number[] | undefined) ??
    [];
  const sessions = payload.sessions as string[] | undefined;
  const correctionsBefore =
    (payload.corrections_before as number | undefined) ??
    (payload.correctionsBefore as number | undefined);
  const correctionsAfter =
    (payload.corrections_after as number | undefined) ??
    (payload.correctionsAfter as number | undefined);

  return {
    source,
    correctionRate: Array.isArray(series) ? series : [],
    sessions,
    correctionsBefore,
    correctionsAfter,
  };
}

export { ApiClientError, ApiConflictError };
