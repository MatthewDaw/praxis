/**
 * Client + model logic for the compounding-loop surface that the agent factory
 * drives over MCP, exposed to a human operator:
 *
 *  - H1 trust/outcome:  GET /context utility + POST /facts/{id}/outcome
 *  - H5 derivations:    GET /facts/{id}/dependents + GET /derivations/stale
 *  - point-in-time:     GET /context?as_of=
 *  - episodic opt-in:   GET /context?include_episodic=true
 *  - meta read path:    /context hit meta (and candidate-detail fallback)
 *
 * Mirrors the patterns in apiClient.ts (contractHeaders, token/org resolution,
 * tolerant snake/camel normalization).
 */
import { contractHeaders } from "./contract";
import type { ApiDataProviderAuth } from "./apiClient";
import { ApiClientError } from "./apiClient";

async function resolveToken(
  auth?: string | ApiDataProviderAuth,
): Promise<{ token?: string; orgId?: string }> {
  const resolved: ApiDataProviderAuth =
    typeof auth === "string" ? { getToken: async () => auth } : auth ?? {};
  const token = resolved.getToken ? await resolved.getToken() : undefined;
  return { token, orgId: resolved.orgId };
}

async function parseJson(response: Response): Promise<unknown> {
  const raw = await response.text();
  if (!raw.trim()) return {};
  return JSON.parse(raw) as unknown;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function numOrUndef(value: unknown): number | undefined {
  if (value == null || value === "") return undefined;
  const n = Number(value);
  return Number.isNaN(n) ? undefined : n;
}

/**
 * Laplace-smoothed utility for an H1 fact: `(success + 0.5) / (total + 1)`.
 * With no recorded outcomes the fact is *neutral* (1.0): absence of evidence
 * must not penalize a freshly written fact.
 */
export function factUtility(successCount = 0, failureCount = 0): number {
  const total = successCount + failureCount;
  if (total <= 0) return 1.0;
  return (successCount + 0.5) / (total + 1);
}

export type UtilityTone = "trusted" | "neutral" | "risky";

/** Coarse band for badge styling. Neutral covers "no evidence yet" and the mid range. */
export function utilityTone(utility: number, successCount = 0, failureCount = 0): UtilityTone {
  if (successCount + failureCount <= 0) return "neutral";
  if (utility >= 0.6) return "trusted";
  if (utility < 0.4) return "risky";
  return "neutral";
}

export function formatUtility(utility: number): string {
  return utility.toFixed(2);
}

/** A fact's H1 trust counters + derived utility (server may supply utility directly). */
export interface FactTrust {
  successCount: number;
  failureCount: number;
  utility: number;
}

/** Pull H1 trust out of an arbitrary fact/candidate record (snake or camel). */
export function parseFactTrust(raw: unknown): FactTrust | undefined {
  const row = asRecord(raw);
  const success = numOrUndef(row.success_count ?? row.successCount);
  const failure = numOrUndef(row.failure_count ?? row.failureCount);
  if (success === undefined && failure === undefined && row.utility === undefined) {
    return undefined;
  }
  const successCount = success ?? 0;
  const failureCount = failure ?? 0;
  const utility = numOrUndef(row.utility) ?? factUtility(successCount, failureCount);
  return { successCount, failureCount, utility };
}

/** One retrieval hit from GET /context. */
export interface ContextHit {
  id: string;
  text: string;
  source: string;
  scope: string;
  category: string;
  trust?: FactTrust;
  meta?: Record<string, unknown>;
}

export function contextHitFromWire(raw: unknown): ContextHit {
  const row = asRecord(raw);
  const metaRaw = row.meta;
  const meta =
    metaRaw && typeof metaRaw === "object" && Object.keys(metaRaw as object).length > 0
      ? (metaRaw as Record<string, unknown>)
      : undefined;
  return {
    id: String(row.id ?? ""),
    text: typeof row.text === "string" ? row.text : String(row.content ?? ""),
    source: typeof row.source === "string" ? row.source : String(row.provenance ?? ""),
    scope: typeof row.scope === "string" ? row.scope : "",
    category: typeof row.category === "string" ? row.category : "",
    trust: parseFactTrust(row),
    meta,
  };
}

export interface ContextQuery {
  query: string;
  topK?: number;
  /** ISO timestamp to pin a point-in-time snapshot (threaded as `as_of`). */
  asOf?: string;
  /** Opt episodic (store-only) facts back into the result set. */
  includeEpisodic?: boolean;
}

/** Build the `/context` query string, omitting empty params and defaulting include_episodic off. */
export function buildContextQueryString({
  query,
  topK,
  asOf,
  includeEpisodic,
}: ContextQuery): string {
  const params = new URLSearchParams();
  params.set("query", query);
  if (topK != null) params.set("top_k", String(topK));
  if (asOf && asOf.trim()) params.set("as_of", asOf.trim());
  // Backend default-excludes episodic; only send the param when opting back in.
  if (includeEpisodic) params.set("include_episodic", "true");
  return params.toString();
}

async function getJson(
  apiBaseUrl: string,
  path: string,
  auth?: string | ApiDataProviderAuth,
): Promise<unknown> {
  const root = apiBaseUrl.replace(/\/$/, "");
  const { token, orgId } = await resolveToken(auth);
  const response = await fetch(`${root}${path}`, {
    headers: contractHeaders(token, orgId),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new ApiClientError(
      `API GET ${path} failed (${response.status}): ${detail || response.statusText}`,
      response.status,
    );
  }
  return parseJson(response);
}

function hitList(payload: unknown): unknown[] {
  if (Array.isArray(payload)) return payload;
  const row = asRecord(payload);
  if (Array.isArray(row.hits)) return row.hits;
  if (Array.isArray(row.results)) return row.results;
  return [];
}

/** `GET /context?query=&top_k=&as_of=&include_episodic=` — retrieval hits. */
export async function getContext(
  apiBaseUrl: string,
  query: ContextQuery,
  auth?: string | ApiDataProviderAuth,
): Promise<ContextHit[]> {
  const qs = buildContextQueryString(query);
  const payload = await getJson(apiBaseUrl, `/context?${qs}`, auth);
  return hitList(payload).map(contextHitFromWire);
}

/** `POST /facts/{id}/outcome {success}` — record an H1 outcome; returns updated trust. */
export async function recordFactOutcome(
  apiBaseUrl: string,
  factId: string,
  success: boolean,
  auth?: string | ApiDataProviderAuth,
): Promise<FactTrust> {
  const root = apiBaseUrl.replace(/\/$/, "");
  const { token, orgId } = await resolveToken(auth);
  const response = await fetch(
    `${root}/facts/${encodeURIComponent(factId)}/outcome`,
    {
      method: "POST",
      headers: contractHeaders(token, orgId),
      body: JSON.stringify({ success }),
    },
  );
  if (!response.ok) {
    const detail = await response.text();
    throw new ApiClientError(
      `API POST /facts/${factId}/outcome failed (${response.status}): ${detail || response.statusText}`,
      response.status,
    );
  }
  return parseFactTrust(await parseJson(response)) ?? { successCount: 0, failureCount: 0, utility: 1.0 };
}

/** A downstream learning derived (kind="derived_from") from a fact. */
export interface FactDependent {
  id: string;
  text: string;
  scope: string;
  /** True when this dependent's source has been invalidated (stale). */
  stale: boolean;
}

export function factDependentFromWire(raw: unknown): FactDependent {
  const row = asRecord(raw);
  return {
    id: String(row.id ?? ""),
    text: typeof row.text === "string" ? row.text : String(row.content ?? ""),
    scope: typeof row.scope === "string" ? row.scope : "",
    stale: Boolean(row.stale ?? row.is_stale ?? row.isStale),
  };
}

function dependentList(payload: unknown): unknown[] {
  if (Array.isArray(payload)) return payload;
  const row = asRecord(payload);
  if (Array.isArray(row.dependents)) return row.dependents;
  return [];
}

/** `GET /facts/{id}/dependents` — downstream learnings derived from this fact. */
export async function getFactDependents(
  apiBaseUrl: string,
  factId: string,
  auth?: string | ApiDataProviderAuth,
): Promise<FactDependent[]> {
  const payload = await getJson(
    apiBaseUrl,
    `/facts/${encodeURIComponent(factId)}/dependents`,
    auth,
  );
  return dependentList(payload).map(factDependentFromWire);
}

/** A fact flagged stale because a `derived_from` source was invalidated. */
export interface StaleDerivation {
  id: string;
  text: string;
  /** The invalidated source fact id(s) this learning derived from. */
  sourceIds: string[];
}

export function staleDerivationFromWire(raw: unknown): StaleDerivation {
  const row = asRecord(raw);
  const rawSources = row.source_ids ?? row.sourceIds ?? row.sources;
  const sourceIds = Array.isArray(rawSources)
    ? rawSources.map((s) => (typeof s === "string" ? s : String(asRecord(s).id ?? "")))
        .filter((s) => s)
    : [];
  return {
    id: String(row.id ?? ""),
    text: typeof row.text === "string" ? row.text : String(row.content ?? ""),
    sourceIds,
  };
}

function staleList(payload: unknown): unknown[] {
  if (Array.isArray(payload)) return payload;
  const row = asRecord(payload);
  if (Array.isArray(row.stale)) return row.stale;
  if (Array.isArray(row.derivations)) return row.derivations;
  return [];
}

/** `GET /derivations/stale` — facts whose `derived_from` source was invalidated. */
export async function getStaleDerivations(
  apiBaseUrl: string,
  auth?: string | ApiDataProviderAuth,
): Promise<StaleDerivation[]> {
  const payload = await getJson(apiBaseUrl, "/derivations/stale", auth);
  return staleList(payload).map(staleDerivationFromWire);
}
