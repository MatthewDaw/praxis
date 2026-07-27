// @vitest-environment jsdom
//
// Acceptance (ticket 3d23de252b7a434b8695372c4c61d94d / R51): given three grouped jobs and two
// ungrouped jobs in the list, the grouped three are visibly associated as one batch and
// distinguishable from the ungrouped ones without opening any job. This test renders the list once
// (no click, no navigation -- nothing is "opened") and asserts the DOM structure itself carries that
// distinction: the three grouped rows share one batch container the two ungrouped rows are not
// inside, and the batch container carries a visible label the ungrouped rows lack.
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { JobList } from "./JobList";
import type { Job } from "./jobGroups";

afterEach(cleanup);

const JOBS: Job[] = [
  { id: "job-a", title: "repo-a: sync deps", state: "running", groupId: "batch-1" },
  { id: "job-x", title: "repo-x: unrelated fix", state: "running", groupId: null },
  { id: "job-b", title: "repo-a: run migration", state: "queued", groupId: "batch-1" },
  { id: "job-y", title: "repo-y: unrelated cleanup", state: "completed" },
  { id: "job-c", title: "repo-a: deploy", state: "queued", groupId: "batch-1" },
];

describe("JobList grouping (R51 acceptance)", () => {
  it("visibly associates the three grouped jobs as one batch", () => {
    render(<JobList jobs={JOBS} />);

    const batch = screen.getByTestId("job-batch-batch-1");
    expect(batch).toBeInTheDocument();
    expect(batch).toHaveClass("job-batch");

    // All three grouped rows render *inside* the shared batch container.
    for (const id of ["job-a", "job-b", "job-c"]) {
      const row = screen.getByTestId(`job-row-${id}`);
      expect(batch).toContainElement(row);
      expect(row).toHaveClass("job-row--grouped");
    }

    // The batch carries a visible label naming it a batch, distinct from a plain row.
    expect(batch).toHaveTextContent(/batch/i);
    expect(batch).toHaveTextContent("3");
  });

  it("renders the two ungrouped jobs outside any batch and undecorated, without opening either", () => {
    render(<JobList jobs={JOBS} />);

    const batch = screen.getByTestId("job-batch-batch-1");
    for (const id of ["job-x", "job-y"]) {
      const row = screen.getByTestId(`job-row-${id}`);
      expect(batch).not.toContainElement(row);
      expect(row).not.toHaveClass("job-row--grouped");
      expect(row.closest(".job-batch")).toBeNull();
    }

    // Nothing here required a click/expand -- the distinction is visible from the plain render.
    expect(screen.queryByRole("button", { name: /open/i })).not.toBeInTheDocument();
  });
});
