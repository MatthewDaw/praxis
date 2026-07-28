/**
 * The website half of R80's job view (the s-jobs surface): a completed job's row carries its
 * branch and pull-request URL, and a failed or needs-attention job's row carries its
 * machine-readable failure reason together with the output of the command that produced it.
 *
 * Mirrors the backend projection `knowledge/serve/box_service_models.py::job_view` field for
 * field, so the dashboard never fabricates a field the job row doesn't actually carry — a field
 * is present only once the job has reached the state that field applies to.
 */

import type { JobState } from "./resumeAction";

export type { JobState };

/** The job row as read from the box-service API — every job-view field is optional since it is
 * only ever populated once the job reaches the state that field applies to. */
export interface JobRow {
  id: string;
  state: JobState;
  /** R89: the model backend active when this job was launched (sonnet|deepseek|unknown). */
  modelBackend?: string;
  branch?: string | null;
  prUrl?: string | null;
  failureReason?: string | null;
  commandOutput?: string | null;
}

/** The projected fields the job view exposes for one job row. */
export interface JobView {
  id: string;
  state: JobState;
  /** R89: the model backend active when this job was launched (sonnet|deepseek|unknown). */
  modelBackend: string;
  branch?: string;
  prUrl?: string;
  failureReason?: string;
  commandOutput?: string;
}

const FAILED_STATES: readonly JobState[] = ["failed", "needs-attention"];

/**
 * Project `job` into exactly the fields the job view exposes for its current state: branch +
 * pull-request URL once `completed`, or failure reason + failing command's output once
 * `failed`/`needs-attention`. Every other state (still in flight) exposes neither pair.
 */
export function jobView(job: JobRow): JobView {
  const view: JobView = {
    id: job.id,
    state: job.state,
    // R89: surface the model backend in the per-job detail — defaults to "unknown"
    // for jobs launched before this field existed, never a false default.
    modelBackend: job.modelBackend || "unknown",
  };
  if (job.state === "completed") {
    view.branch = job.branch ?? undefined;
    view.prUrl = job.prUrl ?? undefined;
  } else if (FAILED_STATES.includes(job.state)) {
    view.failureReason = job.failureReason ?? undefined;
    view.commandOutput = job.commandOutput ?? undefined;
  }
  return view;
}
