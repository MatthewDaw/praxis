import {
  buildCreateBody,
  buildPromoteBody,
  buildPromoteBodyImplicit,
  buildRejectBody,
  buildResolveBody,
  buildUpdateBody,
  contractHeaders,
} from "./contract";
import {
  candidateFromMapping,
  parseCandidateList,
} from "./candidateModel";
import {
  deriveGraphFromCandidates,
  parseGraphPayload,
} from "./graphModel";
import type { DataProvider } from "./dataProvider";
import type { CandidateWriteInput, EvalMetrics } from "../types/candidate";

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

class GraphIngestUnavailableError extends Error {
  statusCode: number;

  constructor(message: string, statusCode: number) {
    super(message);
    this.name = "GraphIngestUnavailableError";
    this.statusCode = statusCode;
  }
}

class EvalRegenerateUnavailableError extends Error {
  statusCode: number;

  constructor(message: string, statusCode: number) {
    super(message);
    this.name = "EvalRegenerateUnavailableError";
    this.statusCode = statusCode;
  }
}

export interface GraphIngestResult {
  summary: string;
  action: string;
  id: string | null;
}

export interface EvalRegenerateResult {
  preset: string;
  casesRun: number;
  casesSkipped: number;
  insightsGenerated: number;
  candidatesInserted: number;
  ranAt: string;
}

function extractCandidateId(path: string): string | undefined {
  const prefix = "/candidates/";
  if (!path.includes(prefix)) {
    return undefined;
  }
  const segment = path.split(prefix)[1]?.split("/")[0];
  return segment ? decodeURIComponent(segment) : undefined;
}

function isPromoteConflict(error: ApiClientError): boolean {
  return (
    error.statusCode === 400 &&
    error.message.toLowerCase().includes("cannot promote")
  );
}

function toPromoteConflict(error: ApiClientError, candidateId: string): ApiConflictError {
  return new ApiConflictError(error.message, candidateId);
}

async function parseJsonResponse(response: Response): Promise<unknown> {
  const raw = await response.text();
  if (!raw.trim()) {
    return {};
  }
  return JSON.parse(raw) as unknown;
}

function responseDetail(detail: string, fallback: string): string {
  if (!detail.trim()) {
    return fallback;
  }
  try {
    const parsed = JSON.parse(detail) as unknown;
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      return String((parsed as { detail: unknown }).detail);
    }
  } catch {
    /* response body was plain text */
  }
  return detail;
}

function normalizeGraphIngestResult(payload: unknown): GraphIngestResult {
  if (!payload || typeof payload !== "object") {
    return { summary: "ingested insight", action: "added", id: null };
  }
  const row = payload as Record<string, unknown>;
  return {
    summary:
      typeof row.summary === "string" && row.summary.trim()
        ? row.summary
        : "ingested insight",
    action:
      typeof row.action === "string" && row.action.trim()
        ? row.action
        : "unknown",
    id: typeof row.id === "string" && row.id.trim() ? row.id : null,
  };
}

function normalizeEvalRegenerateResult(payload: unknown): EvalRegenerateResult {
  const row =
    payload && typeof payload === "object"
      ? (payload as Record<string, unknown>)
      : {};
  return {
    preset: typeof row.preset === "string" ? row.preset : "offline-fake",
    casesRun: Number(row.cases_run ?? row.casesRun ?? 0),
    casesSkipped: Number(row.cases_skipped ?? row.casesSkipped ?? 0),
    insightsGenerated: Number(row.insights_generated ?? row.insightsGenerated ?? 0),
    candidatesInserted: Number(row.candidates_inserted ?? row.candidatesInserted ?? 0),
    ranAt: typeof row.ran_at === "string" ? row.ran_at : String(row.ranAt ?? ""),
  };
}

export interface ApiDataProviderAuth {
  /** Resolve a currently-valid bearer token (Amplify refreshes on demand). */
  getToken?: () => Promise<string | undefined>;
  /** Active org id sent as X-Praxis-Org for server-side tenancy. */
  orgId?: string;
}

export function createApiDataProvider(
  baseUrl: string,
  auth?: ApiDataProviderAuth,
  evalMetricsUrl?: string,
): DataProvider {
  const root = baseUrl.replace(/\/$/, "");
  const metricsUrl =
    evalMetricsUrl?.trim() ||
    import.meta.env.VITE_PRAXIS_EVAL_METRICS_URL?.trim() ||
    `${root}/metrics`;

  async function authHeaders(): Promise<HeadersInit> {
    const token = auth?.getToken ? await auth.getToken() : undefined;
    return contractHeaders(token, auth?.orgId);
  }

  async function request(
    method: string,
    path: string,
    body?: Record<string, unknown>,
  ): Promise<unknown> {
    const response = await fetch(`${root}${path}`, {
      method,
      headers: await authHeaders(),
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
        if (error instanceof ApiClientError && isPromoteConflict(error)) {
          throw toPromoteConflict(error, id);
        }
        if (
          error instanceof ApiClientError &&
          (error.statusCode === 400 || error.statusCode === 422)
        ) {
          try {
            const payload = await request("POST", path, buildPromoteBodyImplicit());
            return candidateFromMapping(payload as Record<string, unknown>);
          } catch (retryError) {
            if (
              retryError instanceof ApiClientError &&
              isPromoteConflict(retryError)
            ) {
              throw toPromoteConflict(retryError, id);
            }
            throw retryError;
          }
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

    async createCandidate(input: CandidateWriteInput) {
      const payload = await request("POST", "/candidates", buildCreateBody(input));
      return candidateFromMapping(payload as Record<string, unknown>);
    },

    async updateCandidate(id, input) {
      const payload = await request(
        "PATCH",
        `/candidates/${encodeURIComponent(id)}`,
        buildUpdateBody(input),
      );
      return candidateFromMapping(payload as Record<string, unknown>);
    },

    async deleteCandidate(id) {
      await request("DELETE", `/candidates/${encodeURIComponent(id)}`);
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
      try {
        const response = await fetch(metricsUrl, {
          headers: await authHeaders(),
        });
        if (!response.ok) {
          throw new Error(response.statusText);
        }
        const payload = (await response.json()) as Record<string, unknown>;
        return normalizeEvalMetrics(payload, metricsUrl);
      } catch (error) {
        const message =
          error instanceof Error ? error.message : "Eval metrics unavailable";
        return {
          source: "placeholder",
          correctionRate: [1.0, 0.72, 0.48, 0.35],
          sessions: ["cold", "run_1", "run_2", "run_3"],
          correctionsBefore: 12,
          correctionsAfter: 5,
          fetchError: message,
        };
      }
    },

    async getGraph() {
      try {
        const payload = await request("GET", "/graph");
        return parseGraphPayload(payload, "api");
      } catch (error) {
        if (
          error instanceof ApiClientError &&
          (error.statusCode === 404 || error.statusCode === 405)
        ) {
          const rows = await this.listCandidates();
          return deriveGraphFromCandidates(rows);
        }
        if (error instanceof ApiClientError) {
          const rows = await this.listCandidates();
          return deriveGraphFromCandidates(rows);
        }
        throw error;
      }
    },

    async getTranscript() {
      return null;
    },
  };
}

export async function postIngestJsonl(
  apiBaseUrl: string,
  files: Array<{ name: string; content: string }>,
  auth?: string | ApiDataProviderAuth,
): Promise<void> {
  const root = apiBaseUrl.replace(/\/$/, "");
  const resolved: ApiDataProviderAuth =
    typeof auth === "string" ? { getToken: async () => auth } : auth ?? {};
  const token = resolved.getToken ? await resolved.getToken() : undefined;
  const response = await fetch(`${root}/ingest/jsonl`, {
    method: "POST",
    headers: contractHeaders(token, resolved.orgId),
    body: JSON.stringify({ files }),
  });

  if (!response.ok) {
    const detail = await response.text();
    if (response.status === 404 || response.status === 405) {
      throw new Error("Distillation endpoint not available yet");
    }
    throw new ApiClientError(
      `API POST /ingest/jsonl failed (${response.status}): ${detail || response.statusText}`,
      response.status,
    );
  }
}

export async function postInsight(
  apiBaseUrl: string,
  insight: string,
  auth?: string | ApiDataProviderAuth,
): Promise<GraphIngestResult> {
  const text = insight.trim();
  if (!text) {
    throw new Error("Insight text required for graph ingest");
  }

  const root = apiBaseUrl.replace(/\/$/, "");
  const resolved: ApiDataProviderAuth =
    typeof auth === "string" ? { getToken: async () => auth } : auth ?? {};
  const token = resolved.getToken ? await resolved.getToken() : undefined;
  const response = await fetch(`${root}/insights`, {
    method: "POST",
    headers: contractHeaders(token, resolved.orgId),
    body: JSON.stringify({ insight: text }),
  });

  if (!response.ok) {
    const detail = await response.text();
    const message = responseDetail(detail, response.statusText);
    if (response.status === 404 || response.status === 405) {
      throw new GraphIngestUnavailableError(
        "Graph ingest endpoint not available yet",
        response.status,
      );
    }
    if (response.status === 503) {
      throw new GraphIngestUnavailableError(message, response.status);
    }
    throw new ApiClientError(
      `API POST /insights failed (${response.status}): ${message}`,
      response.status,
    );
  }

  return normalizeGraphIngestResult(await parseJsonResponse(response));
}

export async function postRegenerateEvals(
  apiBaseUrl: string,
  preset = "offline-fake",
  auth?: string | ApiDataProviderAuth,
): Promise<EvalRegenerateResult> {
  const root = apiBaseUrl.replace(/\/$/, "");
  const resolved: ApiDataProviderAuth =
    typeof auth === "string" ? { getToken: async () => auth } : auth ?? {};
  const token = resolved.getToken ? await resolved.getToken() : undefined;
  const response = await fetch(`${root}/evals/regenerate`, {
    method: "POST",
    headers: contractHeaders(token, resolved.orgId),
    body: JSON.stringify({ preset }),
  });

  if (!response.ok) {
    const detail = await response.text();
    const message = responseDetail(detail, response.statusText);
    if (response.status === 404 || response.status === 405 || response.status === 503) {
      throw new EvalRegenerateUnavailableError(message, response.status);
    }
    throw new ApiClientError(
      `API POST /evals/regenerate failed (${response.status}): ${message}`,
      response.status,
    );
  }

  return normalizeEvalRegenerateResult(await parseJsonResponse(response));
}

export interface EvalScope {
  scope: string;
  caseCount: number;
}

export interface EvalScopesResponse {
  scopes: EvalScope[];
  backends: string[];
  overrideFields: Record<string, string[] | null>;
}

export interface EvalCaseResult {
  caseId: string;
  status: string;
  passed?: boolean;
  rubricScore?: number | null;
  checks?: { name: string; passed: boolean; evidence: string }[];
  output?: string;
  injectedKnowledge?: string | null;
  skipReasons?: string[];
  xfailReason?: string | null;
  error?: string;
}

export interface EvalRunResponse {
  scope: string;
  backend: string;
  overrides: Record<string, unknown>;
  casesRun: number;
  results: EvalCaseResult[];
}

async function resolveToken(
  auth?: string | ApiDataProviderAuth,
): Promise<{ token?: string; orgId?: string }> {
  const resolved: ApiDataProviderAuth =
    typeof auth === "string" ? { getToken: async () => auth } : auth ?? {};
  const token = resolved.getToken ? await resolved.getToken() : undefined;
  return { token, orgId: resolved.orgId };
}

export async function listEvalScopes(
  apiBaseUrl: string,
  auth?: string | ApiDataProviderAuth,
): Promise<EvalScopesResponse> {
  const root = apiBaseUrl.replace(/\/$/, "");
  const { token, orgId } = await resolveToken(auth);
  const response = await fetch(`${root}/evals/scopes`, {
    headers: contractHeaders(token, orgId),
  });
  if (!response.ok) {
    const message = responseDetail(await response.text(), response.statusText);
    throw new ApiClientError(
      `API GET /evals/scopes failed (${response.status}): ${message}`,
      response.status,
    );
  }
  const payload = (await parseJsonResponse(response)) as Partial<EvalScopesResponse>;
  return {
    scopes: Array.isArray(payload.scopes) ? payload.scopes : [],
    backends: Array.isArray(payload.backends) ? payload.backends : [],
    overrideFields: payload.overrideFields ?? {},
  };
}

export async function runEvalScope(
  apiBaseUrl: string,
  scope: string,
  backend: string,
  overrides: Record<string, unknown>,
  auth?: string | ApiDataProviderAuth,
): Promise<EvalRunResponse> {
  const root = apiBaseUrl.replace(/\/$/, "");
  const { token, orgId } = await resolveToken(auth);
  const response = await fetch(`${root}/evals/run`, {
    method: "POST",
    headers: contractHeaders(token, orgId),
    body: JSON.stringify({ scope, backend, overrides }),
  });
  if (!response.ok) {
    const message = responseDetail(await response.text(), response.statusText);
    throw new ApiClientError(
      `API POST /evals/run failed (${response.status}): ${message}`,
      response.status,
    );
  }
  return (await parseJsonResponse(response)) as EvalRunResponse;
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

export {
  ApiClientError,
  ApiConflictError,
  EvalRegenerateUnavailableError,
  GraphIngestUnavailableError,
};
