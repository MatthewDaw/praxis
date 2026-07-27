import { describe, expect, it, vi } from "vitest";

import {
  canCancelJob,
  CANCELLABLE_JOB_STATES,
  CancelActionError,
  triggerCancelJob,
  type JobState,
} from "./cancelAction";

const AUTH = { getToken: async () => "token-123", orgId: "org-1" };

describe("canCancelJob", () => {
  it("is true for every open state", () => {
    for (const state of CANCELLABLE_JOB_STATES) {
      expect(canCancelJob(state)).toBe(true);
    }
  });

  it("is false once the job is at rest", () => {
    expect(canCancelJob("completed")).toBe(false);
    expect(canCancelJob("failed")).toBe(false);
    expect(canCancelJob("needs-attention")).toBe(false);
  });

  it("enumerates exactly the open (non-terminal) states", () => {
    const all: JobState[] = [
      "queued",
      "claimed",
      "running",
      "awaiting-human",
      "completed",
      "failed",
      "needs-attention",
    ];
    const cancellable = all.filter(canCancelJob);
    expect(new Set(cancellable)).toEqual(
      new Set(["queued", "claimed", "running", "awaiting-human"]),
    );
  });
});

describe("triggerCancelJob", () => {
  it("POSTs to the job's cancel endpoint with the operator's credentials", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));

    await triggerCancelJob("http://127.0.0.1:8000", "job-42", "running", AUTH, fetchImpl);

    expect(fetchImpl).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/jobs/job-42/cancel",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          Authorization: "Bearer token-123",
          "X-Org-Id": "org-1",
        }),
      }),
    );
  });

  it("refuses client-side for an already-terminal job without a network call", async () => {
    const fetchImpl = vi.fn();

    await expect(
      triggerCancelJob("http://127.0.0.1:8000", "job-42", "needs-attention", AUTH, fetchImpl),
    ).rejects.toThrow(CancelActionError);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("surfaces a non-2xx response as a CancelActionError", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(new Response("job control lease held elsewhere", { status: 409 }));

    await expect(
      triggerCancelJob("http://127.0.0.1:8000", "job-42", "running", AUTH, fetchImpl),
    ).rejects.toThrow(/409/);
  });
});
