/**
 * R51: grouped jobs are visually associated in the job list so the operator can tell that several
 * rows are one batch awaiting a shared barrier rather than several unrelated jobs.
 *
 * `Job.groupId` is the batch key (undefined/null = ungrouped). `segmentJobsByGroup` turns a flat job
 * list into render segments -- a `"batch"` segment collects every member sharing a `groupId` at the
 * position of that group's first appearance, so members are always rendered together even if the
 * source list interleaves them with unrelated jobs; a `"single"` segment is one ungrouped job. The
 * jobs-view component (`JobList`) renders a `"batch"` segment inside one shared container so the
 * operator can tell the batch apart from ungrouped rows without opening any job.
 */

export type JobState =
  | "queued"
  | "claimed"
  | "running"
  | "awaiting-human"
  | "completed"
  | "failed"
  | "needs-attention";

export interface Job {
  id: string;
  title: string;
  state: JobState;
  /** The shared-barrier batch this job belongs to, or null/undefined when it is not grouped. */
  groupId?: string | null;
}

export type JobBatchSegment =
  | { kind: "batch"; groupId: string; jobs: Job[] }
  | { kind: "single"; job: Job };

/** Partition `jobs` into render segments, grouping every job sharing a `groupId` into one batch
 * segment positioned where that group first appears. Order-preserving and stable for equal input. */
export function segmentJobsByGroup(jobs: readonly Job[]): JobBatchSegment[] {
  const segments: JobBatchSegment[] = [];
  const batchIndex = new Map<string, number>();
  for (const job of jobs) {
    const groupId = job.groupId;
    if (!groupId) {
      segments.push({ kind: "single", job });
      continue;
    }
    const idx = batchIndex.get(groupId);
    if (idx === undefined) {
      batchIndex.set(groupId, segments.length);
      segments.push({ kind: "batch", groupId, jobs: [job] });
    } else {
      const segment = segments[idx];
      if (segment.kind === "batch") segment.jobs.push(job);
    }
  }
  return segments;
}
