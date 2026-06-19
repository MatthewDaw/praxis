import type { CandidateState } from "../types/candidate";
import { nextPromotionState } from "./candidateModel";

export const CONTRACT_HEADER = "X-Praxis-Contract";

const RESOLUTION_TO_API: Record<string, string> = {
  keep_primary: "keep_a",
  keep_rival: "keep_b",
  keep_a: "keep_a",
  keep_b: "keep_b",
};

export function contractVersion(): string {
  return import.meta.env.VITE_PRAXIS_CONTRACT_VERSION?.trim() || "1";
}

export function contractHeaders(token?: string): HeadersInit {
  const headers: Record<string, string> = {
    Accept: "application/json",
    "Content-Type": "application/json",
    [CONTRACT_HEADER]: contractVersion(),
  };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
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

export function contradictionPairId(primaryId: string, rivalId: string): string {
  return `${primaryId}__${rivalId}`;
}
