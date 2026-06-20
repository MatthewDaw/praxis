/**
 * Client for Phoenix trace context shown in Candidate Detail.
 *
 * Live mode calls the Monica-owned proxy (frontend/phoenix_proxy) so the
 * Phoenix Bearer key never reaches the browser. Mock and local-logs modes read
 * a static fixture (`/mock-phoenix-traces.json`) so the demo and offline flows
 * render trace context without any network/secret dependency.
 */

import type { DataSourceMode } from "../config/dataSource";
import type {
  PhoenixLink,
  PhoenixTrace,
  PhoenixTracesResponse,
} from "../types/phoenix";

const MOCK_FIXTURE_PATH = "/mock-phoenix-traces.json";

export class PhoenixUnconfiguredError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PhoenixUnconfiguredError";
  }
}

export function phoenixProxyUrl(): string | undefined {
  return import.meta.env.VITE_PRAXIS_PHOENIX_PROXY_URL?.trim() || undefined;
}

export function hasPhoenixLink(link: PhoenixLink): boolean {
  return Boolean(link.traceId?.trim() || link.sessionId?.trim());
}

/** Read the Phoenix identifier(s) off a candidate's extra fields. */
export function phoenixLinkFromExtra(
  extra: Record<string, unknown>,
): PhoenixLink {
  const str = (value: unknown): string | undefined =>
    typeof value === "string" && value.trim() ? value.trim() : undefined;
  return {
    traceId: str(extra.traceId) ?? str(extra.trace_id) ?? str(extra.phoenixTraceId),
    sessionId:
      str(extra.sessionId) ?? str(extra.session_id) ?? str(extra.phoenixSessionId),
    project: str(extra.phoenixProject) ?? str(extra.phoenix_project),
  };
}

type RawMockTrace = PhoenixTrace & { sessionId?: string };

function matchesLink(trace: RawMockTrace, link: PhoenixLink): boolean {
  if (link.traceId && trace.traceId === link.traceId) {
    return true;
  }
  if (link.sessionId && trace.sessionId === link.sessionId) {
    return true;
  }
  return false;
}

async function loadMockTraces(link: PhoenixLink): Promise<PhoenixTracesResponse> {
  const response = await fetch(MOCK_FIXTURE_PATH);
  if (!response.ok) {
    return { project: "mock", phoenixBaseUrl: "", traces: [] };
  }
  const payload = (await response.json()) as {
    project?: string;
    phoenixBaseUrl?: string;
    traces?: RawMockTrace[];
  };
  const all = Array.isArray(payload.traces) ? payload.traces : [];
  const matched = all.filter((trace) => matchesLink(trace, link));
  return {
    project: payload.project ?? "mock",
    phoenixBaseUrl: payload.phoenixBaseUrl ?? "",
    traces: matched,
  };
}

async function fetchFromProxy(link: PhoenixLink): Promise<PhoenixTracesResponse> {
  const base = phoenixProxyUrl();
  if (!base) {
    throw new PhoenixUnconfiguredError(
      "Phoenix proxy URL is not set (VITE_PRAXIS_PHOENIX_PROXY_URL).",
    );
  }
  const params = new URLSearchParams();
  if (link.traceId) {
    params.set("trace_id", link.traceId);
  }
  if (link.sessionId) {
    params.set("session_id", link.sessionId);
  }
  if (link.project) {
    params.set("project", link.project);
  }
  const url = `${base.replace(/\/$/, "")}/phoenix/traces?${params.toString()}`;
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (response.status === 503) {
    throw new PhoenixUnconfiguredError(
      "Phoenix proxy is not configured with a project/key yet.",
    );
  }
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(
      `Phoenix traces request failed (${response.status}): ${detail || response.statusText}`,
    );
  }
  const payload = (await response.json()) as PhoenixTracesResponse;
  return {
    project: payload.project ?? "",
    phoenixBaseUrl: payload.phoenixBaseUrl ?? "",
    traces: Array.isArray(payload.traces) ? payload.traces : [],
  };
}

/**
 * Resolve Phoenix traces for a candidate link. Mock/local-logs modes use the
 * static fixture; live mode calls the proxy. Returns an empty trace list when
 * the candidate has no Phoenix identifier.
 */
export async function fetchPhoenixTraces(
  link: PhoenixLink,
  mode: DataSourceMode,
): Promise<PhoenixTracesResponse> {
  if (!hasPhoenixLink(link)) {
    return { project: "", phoenixBaseUrl: "", traces: [] };
  }
  if (mode === "mock" || mode === "local-logs") {
    return loadMockTraces(link);
  }
  return fetchFromProxy(link);
}
