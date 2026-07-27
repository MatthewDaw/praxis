/**
 * R26: the website's top level lists live jobs ordered so every job needing attention —
 * `awaiting-human`, `failed`, `needs-attention`, or `attentionNeeded: true` from the
 * `GET /jobs` payload (`knowledge/serve/job_listing.py` owns the actual freshness
 * determination; the website never re-derives it from a raw heartbeat) — sorts above
 * every job progressing normally. This mirrors `praxis_list_jobs` (the MCP tool):
 * both surfaces read the SAME `GET /jobs` response, so this module only orders and
 * fetches — it never recomputes `attentionNeeded` itself.
 */

import type { JobState } from "./resumeAction";

export interface JobSummary {
  id: string;
  project: string;
  snapshot: string;
  state: JobState;
  attentionNeeded: boolean;
  failureReason: string | null;
}

/**
 * Stable-sort `jobs` so every `attentionNeeded` job sorts above every job
 * progressing normally, preserving relative order within each group.
 */
export function orderJobsForOperator(jobs: readonly JobSummary[]): JobSummary[] {
  return [...jobs].sort((a, b) => Number(b.attentionNeeded) - Number(a.attentionNeeded));
}

export interface JobsAuth {
  getToken: () => Promise<string>;
  orgId: string;
}

/** Fetch the operator's live jobs, already ordered by `GET /jobs` (R26). */
export async function fetchJobs(
  apiBaseUrl: string,
  auth: JobsAuth,
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
    throw new Error(`fetching jobs failed (${response.status}): ${detail || response.statusText}`);
  }
  const body = (await response.json()) as { jobs: JobSummary[] };
  return orderJobsForOperator(body.jobs);
}
