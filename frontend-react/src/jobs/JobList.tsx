/**
 * R51: renders a flat job list with grouped jobs visibly associated as one batch -- a shared
 * container with a batch label -- while ungrouped jobs render as plain, undecorated rows. See
 * `segmentJobsByGroup` (jobGroups.ts) for the grouping logic this only renders.
 */
import { segmentJobsByGroup, type Job } from "./jobGroups";

interface JobListProps {
  jobs: Job[];
}

function JobRow({ job, grouped }: { job: Job; grouped: boolean }) {
  return (
    <li
      className={`job-row${grouped ? " job-row--grouped" : ""}`}
      data-testid={`job-row-${job.id}`}
    >
      <span className="job-row-title">{job.title}</span>
      <span className="job-row-state">{job.state}</span>
    </li>
  );
}

export function JobList({ jobs }: JobListProps) {
  const segments = segmentJobsByGroup(jobs);
  return (
    <ul className="job-list" aria-label="Jobs">
      {segments.map((segment) =>
        segment.kind === "batch" ? (
          <li
            key={`batch-${segment.groupId}`}
            className="job-batch"
            data-testid={`job-batch-${segment.groupId}`}
          >
            <div className="job-batch-label">Batch · {segment.jobs.length} jobs</div>
            <ul className="job-batch-members">
              {segment.jobs.map((job) => (
                <JobRow key={job.id} job={job} grouped />
              ))}
            </ul>
          </li>
        ) : (
          <JobRow key={segment.job.id} job={segment.job} grouped={false} />
        ),
      )}
    </ul>
  );
}
