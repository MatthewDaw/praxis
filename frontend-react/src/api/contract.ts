import type { CandidateState, CandidateWriteInput } from "../types/candidate";
import { nextPromotionState } from "./candidateModel";

export const CONTRACT_HEADER = "X-Praxis-Contract";
export const ORG_HEADER = "X-Praxis-Org";
export const SPACE_HEADER = "X-Praxis-Space";

const RESOLUTION_TO_API: Record<string, string> = {
  keep_primary: "keep_a",
  keep_rival: "keep_b",
  keep_a: "keep_a",
  keep_b: "keep_b",
};

export function contractVersion(): string {
  return import.meta.env.VITE_PRAXIS_CONTRACT_VERSION?.trim() || "1";
}

export function contractHeaders(
  token?: string,
  orgId?: string,
  spaceId?: string,
): HeadersInit {
  const headers: Record<string, string> = {
    Accept: "application/json",
    "Content-Type": "application/json",
    [CONTRACT_HEADER]: contractVersion(),
  };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (orgId) {
    headers[ORG_HEADER] = orgId;
  }
  // X-Praxis-Space only scopes snapshot/mount ops (and only when paired with
  // X-Praxis-Snapshot); working memory always keys on principal.sub, so a lone
  // space header never selects a working graph — the backend drops it.
  if (spaceId) {
    headers[SPACE_HEADER] = spaceId;
  }
  return headers;
}

export function buildPromoteBody(currentState: CandidateState): { targetState: string } {
  const next = nextPromotionState(currentState);
  if (!next) {
    throw new Error(`Cannot promote from state ${currentState}`);
  }
  return { targetState: next };
}

export function buildPromoteBodyImplicit(): Record<string, never> {
  return {};
}

export function buildRejectBody(reason?: string): { reason?: string } {
  return reason ? { reason } : {};
}

export function buildCreateBody(input: CandidateWriteInput): Record<string, unknown> {
  const body: Record<string, unknown> = {
    title: input.title.trim(),
    content: input.content.trim(),
    state: "proposed",
    confidence: input.confidence ?? 0.5,
  };
  if (input.provenance?.trim()) {
    body.provenance = input.provenance.trim();
  }
  return body;
}

export function buildUpdateBody(input: CandidateWriteInput): Record<string, unknown> {
  const body: Record<string, unknown> = {
    title: input.title.trim(),
    content: input.content.trim(),
  };
  if (input.provenance?.trim()) {
    body.provenance = input.provenance.trim();
  }
  if (input.confidence != null) {
    body.confidence = input.confidence;
  }
  return body;
}

export function normalizeResolution(resolution: string): string {
  const mapped = RESOLUTION_TO_API[resolution];
  if (!mapped) {
    throw new Error(`Unsupported resolution ${resolution}`);
  }
  return mapped;
}

export function buildResolveBody(
  resolution: string,
  keepId: string,
): { resolution: string; keepId: string } {
  const mapped = RESOLUTION_TO_API[resolution];
  if (!mapped) {
    throw new Error(`Unsupported resolution ${resolution}`);
  }
  return { resolution: mapped, keepId };
}

export function buildCustomResolveBody(customText: string): { customText: string } {
  const text = customText.trim();
  if (!text) {
    throw new Error("Custom resolution text is required");
  }
  return { customText: text };
}

export function contradictionPairId(primaryId: string, rivalId: string): string {
  return `${primaryId}__${rivalId}`;
}
