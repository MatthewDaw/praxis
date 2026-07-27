import { describe, expect, it, vi } from "vitest";

import { fetchJobs, JobsFetchError } from "./jobsClient";

const AUTH = { getToken: async () => "token-123", orgId: "org-1" };

describe("fetchJobs", () => {
  it("GETs the jobs endpoint with the operator's credentials and returns ordered jobs", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          jobs: [
            { id: "progressing-1", state: "running", lastActivityAtMs: Date.now() },
            { id: "attention-1", state: "failed" },
          ],
        }),
        { status: 200 },
      ),
    );

    const jobs = await fetchJobs("http://127.0.0.1:8000", AUTH, fetchImpl);

    expect(fetchImpl).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/jobs",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer token-123",
          "X-Org-Id": "org-1",
        }),
      }),
    );
    expect(jobs.map((j) => j.id)).toEqual(["attention-1", "progressing-1"]);
  });

  it("surfaces a non-2xx response as a JobsFetchError", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response("boom", { status: 500 }));

    await expect(fetchJobs("http://127.0.0.1:8000", AUTH, fetchImpl)).rejects.toThrow(JobsFetchError);
  });
});
