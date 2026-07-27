import { describe, expect, it } from "vitest";

import { needsAttention, orderJobsForAttention, SILENCE_THRESHOLD_MS, type JobSummary } from "./jobOrdering";

const NOW = 10_000_000;

describe("needsAttention", () => {
  it("is true for awaiting-human regardless of activity", () => {
    expect(needsAttention({ id: "j1", state: "awaiting-human" }, NOW)).toBe(true);
  });

  it("is true for failed regardless of activity", () => {
    expect(needsAttention({ id: "j1", state: "failed" }, NOW)).toBe(true);
  });

  it("is true for needs-attention", () => {
    expect(needsAttention({ id: "j1", state: "needs-attention" }, NOW)).toBe(true);
  });

  it("is false for a running job with fresh activity", () => {
    expect(needsAttention({ id: "j1", state: "running", lastActivityAtMs: NOW - 1000 }, NOW)).toBe(false);
  });

  it("is true for a running job silent past the threshold", () => {
    const job: JobSummary = { id: "j1", state: "running", lastActivityAtMs: NOW - SILENCE_THRESHOLD_MS - 1 };
    expect(needsAttention(job, NOW)).toBe(true);
  });

  it("is false for a completed job even with no recent activity", () => {
    expect(needsAttention({ id: "j1", state: "completed", lastActivityAtMs: NOW - 100_000_000 }, NOW)).toBe(
      false,
    );
  });

  it("is false for an open job with no observed activity at all", () => {
    expect(needsAttention({ id: "j1", state: "queued" }, NOW)).toBe(false);
  });
});

describe("orderJobsForAttention", () => {
  it("places every attention-needing job above every progressing one, given an interleaved mix", () => {
    const jobs: JobSummary[] = [
      { id: "progressing-1", state: "running", lastActivityAtMs: NOW - 10 },
      { id: "attention-1", state: "awaiting-human" },
      { id: "progressing-2", state: "queued", lastActivityAtMs: NOW - 5 },
      { id: "attention-2", state: "failed" },
      { id: "progressing-3", state: "claimed", lastActivityAtMs: NOW - 20 },
      { id: "attention-3", state: "running", lastActivityAtMs: NOW - SILENCE_THRESHOLD_MS - 500 },
    ];

    const ordered = orderJobsForAttention(jobs, NOW);
    const attentionIdxs = ordered
      .map((j, i) => (needsAttention(j, NOW) ? i : -1))
      .filter((i) => i >= 0);
    const normalIdxs = ordered
      .map((j, i) => (needsAttention(j, NOW) ? -1 : i))
      .filter((i) => i >= 0);

    expect(Math.max(...attentionIdxs)).toBeLessThan(Math.min(...normalIdxs));
    expect(ordered.map((j) => j.id)).toEqual([
      "attention-1",
      "attention-2",
      "attention-3",
      "progressing-1",
      "progressing-2",
      "progressing-3",
    ]);
  });

  it("preserves relative order within each group (stable sort)", () => {
    const jobs: JobSummary[] = [
      { id: "n1", state: "running", lastActivityAtMs: NOW },
      { id: "a1", state: "failed" },
      { id: "n2", state: "queued", lastActivityAtMs: NOW },
      { id: "a2", state: "awaiting-human" },
    ];

    expect(orderJobsForAttention(jobs, NOW).map((j) => j.id)).toEqual(["a1", "a2", "n1", "n2"]);
  });

  it("does not mutate the input array", () => {
    const jobs: JobSummary[] = [
      { id: "n1", state: "running", lastActivityAtMs: NOW },
      { id: "a1", state: "failed" },
    ];
    const copy = [...jobs];

    orderJobsForAttention(jobs, NOW);

    expect(jobs).toEqual(copy);
  });
});
