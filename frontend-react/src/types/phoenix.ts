/**
 * Normalized Phoenix trace shapes returned by the Monica-owned proxy
 * (frontend/phoenix_proxy). Mirrors the proxy's `normalize_trace` output so the
 * Candidate Detail card can render trace context without knowing Phoenix's raw
 * span schema.
 */

export interface PhoenixSpan {
  name: string;
  kind: string;
  latencyMs: number | null;
  statusCode: string | null;
}

export interface PhoenixTokenCounts {
  prompt: number | null;
  completion: number | null;
  total: number | null;
}

export interface PhoenixTrace {
  traceId: string;
  startTime: string | null;
  latencyMs: number | null;
  statusCode: string | null;
  spanCount: number | null;
  tokens: PhoenixTokenCounts;
  model: string | null;
  spans: PhoenixSpan[];
  phoenixUrl: string | null;
}

export interface PhoenixTracesResponse {
  project: string;
  phoenixBaseUrl: string;
  traces: PhoenixTrace[];
}

/** Identifier extracted from a candidate that links it to Phoenix trace(s). */
export interface PhoenixLink {
  traceId?: string;
  sessionId?: string;
  project?: string;
}
