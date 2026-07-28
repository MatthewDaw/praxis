/**
 * The website half of R26's ordering rule (the s-jobs top-level list): jobs needing
 * attention — `awaiting-human`, `failed`, or silently past the silence threshold —
 * sort above jobs progressing normally. Mirrors
 * `knowledge/serve/box_service_jobs_view.py`'s `needs_attention`/`order_jobs_for_view`
 * exactly (same three conditions, same silence threshold) so the website and the MCP
 * tool (`praxis_list_jobs`, backed by the same `/jobs` endpoint) can never disagree on
 * which jobs need attention.
 */

import type { JobState } from "./resumeAction";

/** Mirrors `observability_signals.SILENCE_THRESHOLD_S` (900s), in milliseconds. */
export const SILENCE_THRESHOLD_MS = 900_000;

/** States that unconditionally need attention, regardless of staleness. */
const ATTENTION_STATES: ReadonlySet<JobState> = new Set(["awaiting-human", "failed", "needs-attention"]);

const OPEN_STATES: ReadonlySet<JobState> = new Set(["queued", "claimed", "running", "awaiting-human"]);

export interface JobSummary {
  id: string;
  state: JobState;
  /** Epoch ms of the last observed heartbeat/activity, or the queued timestamp for a
   * job never yet claimed. `undefined` when nothing has been observed yet. */
  lastActivityAtMs?: number;
  /** R89: the model backend active when this job was launched (sonnet|deepseek|unknown). */
  modelBackend?: string;
}

/** True iff `job` needs operator attention — see the module doc for the three conditions. */
export function needsAttention(job: JobSummary, nowMs: number, silenceThresholdMs = SILENCE_THRESHOLD_MS): boolean {
  if (ATTENTION_STATES.has(job.state)) {
    return true;
  }
  if (!OPEN_STATES.has(job.state)) {
    return false; // completed, or another at-rest state not in ATTENTION_STATES
  }
  if (job.lastActivityAtMs === undefined) {
    return false;
  }
  return nowMs - job.lastActivityAtMs > silenceThresholdMs;
}

/**
 * Every job, ordered so every attention-needing job sorts above every job
 * progressing normally. A stable sort: within each group, jobs keep their input
 * relative order.
 */
export function orderJobsForAttention<T extends JobSummary>(
  jobs: readonly T[],
  nowMs: number,
  silenceThresholdMs = SILENCE_THRESHOLD_MS,
): T[] {
  return [...jobs].sort((a, b) => {
    const aAttn = needsAttention(a, nowMs, silenceThresholdMs);
    const bAttn = needsAttention(b, nowMs, silenceThresholdMs);
    return aAttn === bAttn ? 0 : aAttn ? -1 : 1;
  });
}
