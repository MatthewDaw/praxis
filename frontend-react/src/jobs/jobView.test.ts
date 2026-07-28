import { describe, expect, it } from "vitest";

import { jobView } from "./jobView";

describe("jobView", () => {
  it("exposes the branch and PR URL for a completed job", () => {
    const view = jobView({
      id: "job-1",
      state: "completed",
      branch: "job/job-1",
      prUrl: "https://github.com/acme/widgets/pull/7",
    });

    expect(view.branch).toBe("job/job-1");
    expect(view.prUrl).toBe("https://github.com/acme/widgets/pull/7");
    expect(view.failureReason).toBeUndefined();
    expect(view.commandOutput).toBeUndefined();
  });

  it("exposes the failure reason and the failing command's output for a failed job", () => {
    const view = jobView({
      id: "job-2",
      state: "failed",
      failureReason: "session_crashed",
      commandOutput: "Traceback (most recent call last): ...",
    });

    expect(view.failureReason).toBe("session_crashed");
    expect(view.commandOutput).toBe("Traceback (most recent call last): ...");
    expect(view.branch).toBeUndefined();
    expect(view.prUrl).toBeUndefined();
  });

  it("exposes the same failure fields for a needs-attention job", () => {
    const view = jobView({
      id: "job-3",
      state: "needs-attention",
      failureReason: "merge_conflict",
      commandOutput: "CONFLICT (content): Merge conflict in a.py",
    });

    expect(view.failureReason).toBe("merge_conflict");
    expect(view.commandOutput).toContain("Merge conflict");
  });

  it("exposes neither field pair for a job still in flight", () => {
    const view = jobView({ id: "job-4", state: "running" });

    expect(view.branch).toBeUndefined();
    expect(view.prUrl).toBeUndefined();
    expect(view.failureReason).toBeUndefined();
    expect(view.commandOutput).toBeUndefined();
  });

  it("surfaces modelBackend, defaulting to unknown for absent/missing values (R89)", () => {
    // Known backend surfaced as-is
    const deepseek = jobView({ id: "j1", state: "running", modelBackend: "deepseek" });
    expect(deepseek.modelBackend).toBe("deepseek");

    const sonnet = jobView({ id: "j2", state: "completed", modelBackend: "sonnet" });
    expect(sonnet.modelBackend).toBe("sonnet");

    // Absent → "unknown" (never a false default)
    const missing = jobView({ id: "j3", state: "running" });
    expect(missing.modelBackend).toBe("unknown");

    // Empty string → "unknown"
    const empty = jobView({ id: "j4", state: "failed", modelBackend: "" });
    expect(empty.modelBackend).toBe("unknown");
  });
});
