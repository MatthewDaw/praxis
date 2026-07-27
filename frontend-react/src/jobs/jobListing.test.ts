import { describe, expect, it, vi } from "vitest";

import { fetchJobs, orderJobsForOperator, type JobSummary } from "./jobListing";

function job(id: string, attentionNeeded: boolean, state: JobSummary["state"] = "running"): JobSummary {
  return { id, project: "p", snapshot: "prd-p", state, attentionNeeded, failureReason: null };
}

describe("orderJobsForOperator", () => {
  it("places every attention-needing job above every progressing one", () => {
    const jobs = [
      job("progressing-1", false),
      job("attention-1", true, "awaiting-human"),
      job("progressing-2", false, "queued"),
      job("attention-2", true, "failed"),
    ];

    const ordered = orderJobsForOperator(jobs);
    const ids = ordered.map((j) => j.id);
    const attentionIdx = ["attention-1", "attention-2"].map((id) => ids.indexOf(id));
    const progressingIdx = ["progressing-1", "progressing-2"].map((id) => ids.indexOf(id));
    expect(Math.max(...attentionIdx)).toBeLessThan(Math.min(...progressingIdx));
  });

  it("preserves input order within each group (stable sort)", () => {
    const jobs = [job("a", true), job("b", true), job("c", false), job("d", false)];
    expect(orderJobsForOperator(jobs).map((j) => j.id)).toEqual(["a", "b", "c", "d"]);
  });
});

describe("fetchJobs", () => {
  it("GETs /jobs with auth headers and returns the attention-ordered list", async () => {
    const payload = { jobs: [job("progressing", false), job("attention", true, "failed")] };
    const fetchImpl = vi.fn(async () => ({
      ok: true,
      json: async () => payload,
      text: async () => "",
      status: 200,
      statusText: "OK",
    })) as unknown as typeof fetch;

    const result = await fetchJobs(
      "https://api.test/",
      { getToken: async () => "tok", orgId: "org-1" },
      fetchImpl,
    );

    expect(fetchImpl).toHaveBeenCalledWith(
      "https://api.test/jobs",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok", "X-Org-Id": "org-1" },
      }),
    );
    expect(result.map((j) => j.id)).toEqual(["attention", "progressing"]);
  });

  it("throws with the response detail on a non-2xx response", async () => {
    const fetchImpl = vi.fn(async () => ({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      text: async () => "boom",
      json: async () => ({ jobs: [] }),
    })) as unknown as typeof fetch;

    await expect(
      fetchJobs("https://api.test", { getToken: async () => "tok", orgId: "org-1" }, fetchImpl),
    ).rejects.toThrow(/boom/);
  });
});
