import type {
  AuditEntry,
  Candidate,
  CandidateState,
  ConfidenceBreakdown,
  RawCandidate,
} from "../types/candidate";

const KNOWN_KEYS = new Set([
  "id",
  "title",
  "content",
  "state",
  "confidence",
  "provenance",
  "source",
  "source_log",
  "sourceLog",
  "createdAt",
  "created_at",
  "updatedAt",
  "updated_at",
  "confidenceBreakdown",
  "confidence_breakdown",
  "contradictions",
  "contradiction_ids",
  "auditTrail",
  "audit_trail",
]);

const KNOWN_STATES = new Set<CandidateState>([
  "proposed",
  "active",
  "decayed",
]);

export function parseCandidateState(raw: unknown): {
  state: CandidateState;
  displayState: string;
} {
  const label = String(raw ?? "proposed");
  if (KNOWN_STATES.has(label as CandidateState)) {
    return { state: label as CandidateState, displayState: label };
  }
  return { state: "unrecognized", displayState: label };
}

export function nextPromotionState(
  current: CandidateState,
): CandidateState | null {
  switch (current) {
    case "proposed":
      return "active";
    case "active":
    case "decayed":
    case "unrecognized":
      return null;
    default: {
      const _exhaustive: never = current;
      throw new Error(`Unhandled candidate state: ${_exhaustive}`);
    }
  }
}

export function promoteUnavailableReason(candidate: Candidate): string {
  if (nextPromotionState(candidate.state)) {
    return "";
  }
  if (candidate.state === "decayed") {
    return `${candidate.title} is decayed — restore via pipeline before promoting.`;
  }
  return `${candidate.title} is already ${candidate.displayState} — no further promotion.`;
}

export function formatCandidateDate(iso: string): string {
  if (!iso) {
    return "—";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

export interface CandidateStateStyle {
  bg: string;
  text: string;
  border: string;
}

export function candidateStateStyle(state: CandidateState): CandidateStateStyle {
  switch (state) {
    case "proposed":
      return { bg: "#fef3c7", text: "#92400e", border: "#fcd34d" };
    case "active":
      return { bg: "#dcfce7", text: "#166534", border: "#86efac" };
    case "decayed":
    case "unrecognized":
      return { bg: "#f3f4f6", text: "#4b5563", border: "#d1d5db" };
    default: {
      const _exhaustive: never = state;
      throw new Error(`Unhandled candidate state: ${_exhaustive}`);
    }
  }
}

export function candidateStateClass(state: CandidateState): string {
  switch (state) {
    case "proposed":
      return "state-badge--proposed";
    case "active":
      return "state-badge--active";
    case "decayed":
      return "state-badge--decayed";
    case "unrecognized":
      return "state-badge--unrecognized";
    default: {
      const _exhaustive: never = state;
      throw new Error(`Unhandled candidate state: ${_exhaustive}`);
    }
  }
}

/** @deprecated Use candidateStateClass or candidateStateStyle for muted enterprise pills. */
export function candidateStateColor(state: CandidateState): string {
  return candidateStateStyle(state).bg;
}

function firstString(data: RawCandidate, ...keys: string[]): string {
  for (const key of keys) {
    const value = data[key];
    if (value != null && String(value).trim()) {
      return String(value);
    }
  }
  return "";
}

function normalizeContradictionIds(raw: unknown): string[] {
  if (!Array.isArray(raw)) {
    return [];
  }
  const ids: string[] = [];
  for (const item of raw) {
    if (typeof item === "string") {
      ids.push(item);
    } else if (item && typeof item === "object" && "id" in item) {
      ids.push(String((item as { id: unknown }).id));
    }
  }
  return ids;
}

function parseBreakdown(raw: unknown): ConfidenceBreakdown | undefined {
  if (!raw || typeof raw !== "object") {
    return undefined;
  }
  const row = raw as Record<string, unknown>;
  return {
    frequency: Number(row.frequency ?? 0),
    recency: Number(row.recency ?? 0),
    breadth: Number(row.breadth ?? 0),
    frequencyRationale: String(
      row.frequencyRationale ?? row.frequency_rationale ?? "",
    ),
    recencyRationale: String(
      row.recencyRationale ?? row.recency_rationale ?? "",
    ),
    breadthRationale: String(
      row.breadthRationale ?? row.breadth_rationale ?? "",
    ),
  };
}

function parseAuditTrail(raw: unknown): AuditEntry[] {
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw
    .filter((item): item is Record<string, unknown> => !!item && typeof item === "object")
    .map((item) => ({
      action: String(item.action ?? "event"),
      timestamp: String(item.timestamp ?? ""),
      provenance: String(item.provenance ?? ""),
      actor: String(item.actor ?? "system"),
      note: item.note != null ? String(item.note) : undefined,
    }));
}

export function candidateFromMapping(data: RawCandidate): Candidate {
  const { state, displayState } = parseCandidateState(data.state);
  const breakdown = parseBreakdown(
    data.confidenceBreakdown ?? data.confidence_breakdown,
  );
  const contradictions = data.contradictions ?? data.contradiction_ids ?? [];
  const auditTrail = parseAuditTrail(data.auditTrail ?? data.audit_trail);

  const extra: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(data)) {
    if (!KNOWN_KEYS.has(key)) {
      extra[key] = value;
    }
  }

  return {
    id: String(data.id ?? ""),
    title: String(data.title ?? ""),
    content: String(data.content ?? ""),
    state,
    displayState,
    confidence: Number(data.confidence ?? 0),
    provenance: firstString(
      data,
      "provenance",
      "source",
      "source_log",
      "sourceLog",
    ),
    createdAt: firstString(
      data,
      "createdAt",
      "created_at",
      "updatedAt",
      "updated_at",
    ),
    confidenceBreakdown: breakdown,
    contradictionIds: normalizeContradictionIds(contradictions),
    auditTrail,
    extra,
  };
}

export function parseCandidateList(payload: unknown): RawCandidate[] {
  if (Array.isArray(payload)) {
    return payload.filter(
      (row): row is RawCandidate => !!row && typeof row === "object",
    );
  }
  if (payload && typeof payload === "object") {
    const rows = (payload as { candidates?: unknown }).candidates;
    if (Array.isArray(rows)) {
      return rows.filter(
        (row): row is RawCandidate => !!row && typeof row === "object",
      );
    }
  }
  return [];
}
