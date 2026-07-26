import { describe, expect, it, vi } from "vitest";

import {
  canResumeJob,
  RESUMABLE_JOB_STATES,
  ResumeActionError,
  triggerResumeJob,
  type JobState,
} from "./resumeAction";

const AUTH = { getToken: async () => "token-123", orgId: "org-1" };

describe("canResumeJob", () => {
  it("is true for every state other than completed", () => {
    for (const state of RESUMABLE_JOB_STATES) {
      expect(canResumeJob(state)).toBe(true);
    }
  });

  it("is false once the job has completed", () => {
    expect(canResumeJob("completed")).toBe(false);
  });

  it("enumerates exactly the acceptance condition's non-completed states", () => {
    const all: JobState[] = [
      "queued",
      "claimed",
      "running",
      "awaiting-human",
      "completed",
      "failed",
      "needs-attention",
    ];
    const resumable = all.filter(canResumeJob);
    expect(new Set(resumable)).toEqual(
      new Set(["queued", "claimed", "running", "awaiting-human", "failed", "needs-attention"]),
    );
  });
});

describe("triggerResumeJob", () => {
  it("POSTs to the job's resume endpoint with the operator's credentials", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));

    await triggerResumeJob("http://127.0.0.1:8000", "job-42", "failed", AUTH, fetchImpl);

    expect(fetchImpl).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/jobs/job-42/resume",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          Authorization: "Bearer token-123",
          "X-Org-Id": "org-1",
        }),
      }),
    );
  });

  it("refuses client-side for an already-completed job without a network call", async () => {
    const fetchImpl = vi.fn();

    await expect(
      triggerResumeJob("http://127.0.0.1:8000", "job-42", "completed", AUTH, fetchImpl),
    ).rejects.toThrow(ResumeActionError);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("surfaces a non-2xx response as a ResumeActionError", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(new Response("job control lease held elsewhere", { status: 409 }));

    await expect(
      triggerResumeJob("http://127.0.0.1:8000", "job-42", "failed", AUTH, fetchImpl),
    ).rejects.toThrow(/409/);
  });
});
