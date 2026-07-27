/**
 * The website half of R77's cancel action (the s-jobs surface).
 *
 * A remote job is cancellable iff it is still "open" — `queued`, `claimed`, `running`, or
 * `awaiting-human` — the same enumeration `knowledge/serve/box_service_models.py`'s
 * `OPEN_JOB_STATES` and `box_service_cancel.can_cancel` key on. `canCancelJob` is the single place
 * both the jobs-view button's enabled state and any future confirmation copy read that rule from,
 * so the website and the backend can never drift on which states are cancellable.
 *
 * `triggerCancelJob` is a thin POST to the job control surface — the operator-facing sibling of an
 * equivalent MCP tool call, both ultimately invoking the same box-service `cancel_job` action. It
 * throws `CancelActionError` on any non-2xx response so a caller (a button handler) can surface the
 * failure rather than silently no-op.
 */

export type JobState =
  | "queued"
  | "claimed"
  | "running"
  | "awaiting-human"
  | "completed"
  | "failed"
  | "needs-attention";

/** The open states — the only ones a job can be cancelled from (mirrors `OPEN_JOB_STATES`). */
export const CANCELLABLE_JOB_STATES: readonly JobState[] = [
  "queued",
  "claimed",
  "running",
  "awaiting-human",
];

export function canCancelJob(state: JobState): boolean {
  return (CANCELLABLE_JOB_STATES as readonly string[]).includes(state);
}

export class CancelActionError extends Error {
  readonly statusCode: number;

  constructor(message: string, statusCode: number) {
    super(message);
    this.name = "CancelActionError";
    this.statusCode = statusCode;
  }
}

export interface CancelAuth {
  getToken: () => Promise<string>;
  orgId: string;
}

/**
 * Trigger a cancel for `jobId`. Refuses client-side (without a network call) when `currentState`
 * is already at rest (`completed`, `failed`, or `needs-attention`), mirroring the backend's own
 * refusal (`box_service_cancel.CancelError`) so the operator gets the same answer whichever surface
 * asks.
 */
export async function triggerCancelJob(
  apiBaseUrl: string,
  jobId: string,
  currentState: JobState,
  auth: CancelAuth,
  fetchImpl: typeof fetch = fetch,
): Promise<void> {
  if (!canCancelJob(currentState)) {
    throw new CancelActionError(`job ${jobId} is already at rest — nothing to cancel`, 409);
  }

  const root = apiBaseUrl.replace(/\/$/, "");
  const token = await auth.getToken();
  const response = await fetchImpl(`${root}/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      "X-Org-Id": auth.orgId,
    },
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new CancelActionError(
      `cancel of job ${jobId} failed (${response.status}): ${detail || response.statusText}`,
      response.status,
    );
  }
}
