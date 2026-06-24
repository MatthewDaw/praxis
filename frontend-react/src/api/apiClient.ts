import {
  buildCreateBody,
  buildCustomResolveBody,
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
import type { DataProvider, Snapshot } from "./dataProvider";
import type { CandidateWriteInput } from "../types/candidate";

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

/** Result of `POST /evals/regenerate` (cache-only, does not change the live graph). */
export interface EvalCacheResult {
  casesCached: number;
  regenerated: string[];
  fromCache: string[];
  ranAt: string;
}

/** Result of `POST /evals/load` (puts cached eval data into the live graph). */
export interface EvalLoadResult {
  mode: "add" | "replace";
  regenerated: string[];
  fromCache: string[];
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

function toStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((v): v is string => typeof v === "string");
}

function normalizeEvalCacheResult(payload: unknown): EvalCacheResult {
  const row =
    payload && typeof payload === "object"
      ? (payload as Record<string, unknown>)
      : {};
  return {
    casesCached: Number(row.cases_cached ?? row.casesCached ?? 0),
    regenerated: toStringArray(row.regenerated),
    fromCache: toStringArray(row.from_cache ?? row.fromCache),
    ranAt: typeof row.ran_at === "string" ? row.ran_at : String(row.ranAt ?? ""),
  };
}

function normalizeEvalLoadResult(payload: unknown): EvalLoadResult {
  const row =
    payload && typeof payload === "object"
      ? (payload as Record<string, unknown>)
      : {};
  const mode = row.mode === "replace" ? "replace" : "add";
  return {
    mode,
    regenerated: toStringArray(row.regenerated),
    fromCache: toStringArray(row.from_cache ?? row.fromCache),
    candidatesInserted: Number(row.candidates_inserted ?? row.candidatesInserted ?? 0),
    ranAt: typeof row.ran_at === "string" ? row.ran_at : String(row.ranAt ?? ""),
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

function normalizeSnapshot(payload: unknown): Snapshot {
  const row =
    payload && typeof payload === "object"
      ? (payload as Record<string, unknown>)
      : {};
  return {
    name: typeof row.name === "string" ? row.name : "",
    count: Number(row.count ?? 0),
    createdAt:
      typeof row.createdAt === "string"
        ? row.createdAt
        : typeof row.created_at === "string"
          ? row.created_at
          : "",
  };
}

function normalizeSnapshotList(payload: unknown): Snapshot[] {
  const row =
    payload && typeof payload === "object"
      ? (payload as Record<string, unknown>)
      : {};
  const list = Array.isArray(row.snapshots) ? row.snapshots : [];
  return list.map(normalizeSnapshot);
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
): DataProvider {
  const root = baseUrl.replace(/\/$/, "");

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

    async resolveContradictionCustom(contradictionId, customText) {
      const payload = await request(
        "POST",
        `/contradictions/${encodeURIComponent(contradictionId)}/resolve`,
        buildCustomResolveBody(customText),
      );
      return candidateFromMapping(payload as Record<string, unknown>);
    },

    async getGraph(state = "active") {
      try {
        const path = state && state !== "active" ? `/graph?state=${encodeURIComponent(state)}` : "/graph";
        const payload = await request("GET", path);
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

    async clearGraph() {
      const payload = await request("POST", "/graph/clear");
      const row =
        payload && typeof payload === "object" ? (payload as Record<string, unknown>) : {};
      return { cleared: Number(row.cleared ?? 0) };
    },

    async getTranscript() {
      return null;
    },

    async listSnapshots() {
      const payload = await request("GET", "/snapshots");
      return normalizeSnapshotList(payload);
    },

    async saveSnapshot(name: string) {
      const payload = await request("POST", "/snapshots", { name });
      return normalizeSnapshot(payload);
    },

    async loadSnapshot(name: string, mode: "add" | "replace" = "replace") {
      const payload = await request(
        "POST",
        `/snapshots/${encodeURIComponent(name)}/load`,
        { mode },
      );
      const row =
        payload && typeof payload === "object"
          ? (payload as Record<string, unknown>)
          : {};
      return { loaded: Number(row.loaded ?? 0) };
    },

    async deleteSnapshot(name: string) {
      const payload = await request(
        "DELETE",
        `/snapshots/${encodeURIComponent(name)}`,
      );
      const row =
        payload && typeof payload === "object"
          ? (payload as Record<string, unknown>)
          : {};
      return { deleted: typeof row.deleted === "string" ? row.deleted : name };
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

/**
 * Cached eval cases as a map of case id → cached node count. Membership in the
 * map means "cached" (green dot); the value is the node count shown inside it.
 */
export async function listCachedEvalCases(
  apiBaseUrl: string,
  auth?: string | ApiDataProviderAuth,
): Promise<Map<string, number>> {
  const root = apiBaseUrl.replace(/\/$/, "");
  const { token, orgId } = await resolveToken(auth);
  const response = await fetch(`${root}/evals/cached`, {
    headers: contractHeaders(token, orgId),
  });
  if (!response.ok) {
    const message = responseDetail(await response.text(), response.statusText);
    throw new ApiClientError(
      `API GET /evals/cached failed (${response.status}): ${message}`,
      response.status,
    );
  }
  const payload = await parseJsonResponse(response);
  const obj = payload && typeof payload === "object" ? (payload as Record<string, unknown>) : {};
  const cached = Array.isArray(obj.cached)
    ? obj.cached.filter((c): c is string => typeof c === "string")
    : [];
  const counts =
    obj.counts && typeof obj.counts === "object" ? (obj.counts as Record<string, unknown>) : {};
  const out = new Map<string, number>();
  for (const id of cached) {
    const n = counts[id];
    out.set(id, typeof n === "number" ? n : 0);
  }
  return out;
}

export interface EvalCachePayload {
  caseIds?: string[];
  scopes?: string[];
  distill?: boolean;
}

export interface EvalLoadPayload {
  caseIds?: string[];
  scopes?: string[];
  mode: "add" | "replace";
  distill?: boolean;
}

/**
 * `POST /evals/regenerate` — create/update the eval CACHE ONLY.
 * Does NOT change the live graph.
 */
export async function regenerateEvalCache(
  apiBaseUrl: string,
  { caseIds, scopes, distill }: EvalCachePayload,
  auth?: string | ApiDataProviderAuth,
): Promise<EvalCacheResult> {
  const root = apiBaseUrl.replace(/\/$/, "");
  const { token, orgId } = await resolveToken(auth);
  const body: Record<string, unknown> = {};
  if (caseIds) body.caseIds = caseIds;
  if (scopes) body.scopes = scopes;
  if (distill !== undefined) body.distill = distill;
  const response = await fetch(`${root}/evals/regenerate`, {
    method: "POST",
    headers: contractHeaders(token, orgId),
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const message = responseDetail(await response.text(), response.statusText);
    if (response.status === 404 || response.status === 405 || response.status === 503) {
      throw new EvalRegenerateUnavailableError(message, response.status);
    }
    throw new ApiClientError(
      `API POST /evals/regenerate failed (${response.status}): ${message}`,
      response.status,
    );
  }
  return normalizeEvalCacheResult(await parseJsonResponse(response));
}

/**
 * `POST /evals/load` — put cached eval data into the live graph
 * (regenerating cache misses first). `mode:"add"` additively upserts each
 * eval's nodes; `mode:"replace"` truncates the whole live graph then inserts.
 */
export async function loadEvals(
  apiBaseUrl: string,
  { caseIds, scopes, mode, distill }: EvalLoadPayload,
  auth?: string | ApiDataProviderAuth,
): Promise<EvalLoadResult> {
  const root = apiBaseUrl.replace(/\/$/, "");
  const { token, orgId } = await resolveToken(auth);
  const body: Record<string, unknown> = { mode };
  if (caseIds) body.caseIds = caseIds;
  if (scopes) body.scopes = scopes;
  if (distill !== undefined) body.distill = distill;
  const response = await fetch(`${root}/evals/load`, {
    method: "POST",
    headers: contractHeaders(token, orgId),
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const message = responseDetail(await response.text(), response.statusText);
    if (response.status === 404 || response.status === 405 || response.status === 503) {
      throw new EvalRegenerateUnavailableError(message, response.status);
    }
    throw new ApiClientError(
      `API POST /evals/load failed (${response.status}): ${message}`,
      response.status,
    );
  }
  return normalizeEvalLoadResult(await parseJsonResponse(response));
}

async function snapshotRequest(
  apiBaseUrl: string,
  method: string,
  path: string,
  auth?: string | ApiDataProviderAuth,
  body?: Record<string, unknown>,
): Promise<unknown> {
  const root = apiBaseUrl.replace(/\/$/, "");
  const { token, orgId } = await resolveToken(auth);
  const response = await fetch(`${root}${path}`, {
    method,
    headers: contractHeaders(token, orgId),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const message = responseDetail(await response.text(), response.statusText);
    throw new ApiClientError(
      `API ${method} ${path} failed (${response.status}): ${message}`,
      response.status,
    );
  }
  return parseJsonResponse(response);
}

export async function listSnapshots(
  apiBaseUrl: string,
  auth?: string | ApiDataProviderAuth,
): Promise<Snapshot[]> {
  return normalizeSnapshotList(
    await snapshotRequest(apiBaseUrl, "GET", "/snapshots", auth),
  );
}

export async function saveSnapshot(
  apiBaseUrl: string,
  name: string,
  auth?: string | ApiDataProviderAuth,
): Promise<Snapshot> {
  return normalizeSnapshot(
    await snapshotRequest(apiBaseUrl, "POST", "/snapshots", auth, { name }),
  );
}

export async function loadSnapshot(
  apiBaseUrl: string,
  name: string,
  mode: "add" | "replace" = "replace",
  auth?: string | ApiDataProviderAuth,
): Promise<{ loaded: number }> {
  const payload = await snapshotRequest(
    apiBaseUrl,
    "POST",
    `/snapshots/${encodeURIComponent(name)}/load`,
    auth,
    { mode },
  );
  const row =
    payload && typeof payload === "object"
      ? (payload as Record<string, unknown>)
      : {};
  return { loaded: Number(row.loaded ?? 0) };
}

export async function deleteSnapshot(
  apiBaseUrl: string,
  name: string,
  auth?: string | ApiDataProviderAuth,
): Promise<{ deleted: string }> {
  const payload = await snapshotRequest(
    apiBaseUrl,
    "DELETE",
    `/snapshots/${encodeURIComponent(name)}`,
    auth,
  );
  const row =
    payload && typeof payload === "object"
      ? (payload as Record<string, unknown>)
      : {};
  return { deleted: typeof row.deleted === "string" ? row.deleted : name };
}

export {
  ApiClientError,
  ApiConflictError,
  EvalRegenerateUnavailableError,
  GraphIngestUnavailableError,
};
