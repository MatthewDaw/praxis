/**
 * The website's fetch half of R26: which jobs are live and their states, for the
 * s-jobs top-level list. Thin GET against the same `/jobs` endpoint the
 * `praxis_list_jobs` MCP tool reads, so both surfaces retrieve identical data
 * (`knowledge/serve/app.py`'s `/jobs` route). Mirrors `resumeAction.ts`'s
 * `triggerResumeJob` shape: an injectable `fetchImpl`, auth via a bearer token +
 * org header, and a typed error on a non-2xx response.
 */

import { orderJobsForAttention, type JobSummary } from "./jobOrdering";
import type { ResumeAuth } from "./resumeAction";

export class JobsFetchError extends Error {
  readonly statusCode: number;

  constructor(message: string, statusCode: number) {
    super(message);
    this.name = "JobsFetchError";
    this.statusCode = statusCode;
  }
}

interface JobsResponse {
  jobs: Array<{
    id: string;
    state: JobSummary["state"];
    lastActivityAtMs?: number;
    /** R89: the model backend active when this job was launched. */
    modelBackend?: string;
  }>;
}

/**
 * Fetch the live jobs list, already ordered so every attention-needing job sorts
 * above every job progressing normally (`orderJobsForAttention`) — the same
 * ordering rule the backend and the MCP tool apply, computed client-side here so
 * the list re-sorts live against the caller's own clock.
 */
export async function fetchJobs(
  apiBaseUrl: string,
  auth: ResumeAuth,
  fetchImpl: typeof fetch = fetch,
): Promise<JobSummary[]> {
  const root = apiBaseUrl.replace(/\/$/, "");
  const token = await auth.getToken();
  const response = await fetchImpl(`${root}/jobs`, {
    headers: {
      Authorization: `Bearer ${token}`,
      "X-Org-Id": auth.orgId,
    },
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new JobsFetchError(
      `fetching jobs failed (${response.status}): ${detail || response.statusText}`,
      response.status,
    );
  }

  const payload = (await response.json()) as JobsResponse;
  return orderJobsForAttention(payload.jobs, Date.now());
}
