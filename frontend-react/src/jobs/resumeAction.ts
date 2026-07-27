/**
 * The website half of R29's resume action (the s-jobs surface).
 *
 * A remote job "did not finish" iff its state is anything other than `"completed"` — the same
 * enumeration the box-service acceptance condition and `knowledge/serve/box_service_resume.py`
 * key on. `canResumeJob` is the single place both the jobs-view button's enabled state and any
 * future confirmation copy read that rule from, so the website and the backend can never drift
 * on which states are resumable.
 *
 * `triggerResumeJob` is a thin POST to the job control surface — the operator-facing sibling of
 * an equivalent MCP tool call, both ultimately invoking the same box-service resume action. It
 * throws `ResumeActionError` on any non-2xx response so a caller (a button handler) can surface
 * the failure rather than silently no-op.
 */

export type JobState =
  | "queued"
  | "claimed"
  | "running"
  | "awaiting-human"
  | "completed"
  | "failed"
  | "needs-attention";

/** Every job state other than `"completed"` — the operator's only signal a job "did not finish". */
export const RESUMABLE_JOB_STATES: readonly JobState[] = [
  "queued",
  "claimed",
  "running",
  "awaiting-human",
  "failed",
  "needs-attention",
];

export function canResumeJob(state: JobState): boolean {
  return state !== "completed";
}

export class ResumeActionError extends Error {
  readonly statusCode: number;

  constructor(message: string, statusCode: number) {
    super(message);
    this.name = "ResumeActionError";
    this.statusCode = statusCode;
  }
}

export interface ResumeAuth {
  getToken: () => Promise<string>;
  orgId: string;
}

/**
 * Trigger a resume for `jobId`. Refuses client-side (without a network call) when `currentState`
 * is already `"completed"`, mirroring the backend's own refusal
 * (`box_service_resume.ResumeError`) so the operator gets the same answer whichever surface asks.
 */
export async function triggerResumeJob(
  apiBaseUrl: string,
  jobId: string,
  currentState: JobState,
  auth: ResumeAuth,
  fetchImpl: typeof fetch = fetch,
): Promise<void> {
  if (!canResumeJob(currentState)) {
    throw new ResumeActionError(`job ${jobId} already completed — nothing to resume`, 409);
  }

  const root = apiBaseUrl.replace(/\/$/, "");
  const token = await auth.getToken();
  const response = await fetchImpl(`${root}/jobs/${encodeURIComponent(jobId)}/resume`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      "X-Org-Id": auth.orgId,
    },
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new ResumeActionError(
      `resume of job ${jobId} failed (${response.status}): ${detail || response.statusText}`,
      response.status,
    );
  }
}
