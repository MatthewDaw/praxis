import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { nextPromotionState, parseCandidateList } from "./candidateModel";
import { createMockDataProviderWithRows } from "./mockProvider";

const REPO_ROOT = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../..",
);

function loadMockCandidates(): ReturnType<typeof parseCandidateList> {
  const path = join(
    REPO_ROOT,
    "frontend-react",
    "public",
    "mock-candidates.json",
  );
  return parseCandidateList(JSON.parse(readFileSync(path, "utf-8")));
}

describe("mock gate workflow", () => {
  it("lists at least 18 mock candidates", async () => {
    const provider = createMockDataProviderWithRows(loadMockCandidates());
    const candidates = await provider.listCandidates();
    expect(candidates.length).toBeGreaterThanOrEqual(18);
  });

  it("promotes proposed to active", async () => {
    const provider = createMockDataProviderWithRows(loadMockCandidates());
    const updated = await provider.promote("cand_1");
    expect(updated.state).toBe("active");
  });

  it("exposes contradiction pair cand_9 and cand_16", async () => {
    const provider = createMockDataProviderWithRows(loadMockCandidates());
    const primary = await provider.getCandidate("cand_9");
    expect(primary).not.toBeNull();
    expect(primary?.contradictionIds).toContain("cand_16");
  });

  it("chains promotion states", () => {
    expect(nextPromotionState("proposed")).toBe("active");
    expect(nextPromotionState("active")).toBeNull();
  });

  it("marks rejected candidates as decayed", async () => {
    const provider = createMockDataProviderWithRows(loadMockCandidates());
    await provider.reject("cand_3", "duplicate lesson");
    const updated = await provider.getCandidate("cand_3");
    expect(updated?.state).toBe("decayed");
  });

  it("treats already-active candidates as terminal for promotion", async () => {
    const provider = createMockDataProviderWithRows(loadMockCandidates());
    const before = await provider.getCandidate("cand_2");
    expect(before?.state).toBe("active");
    expect(nextPromotionState(before!.state)).toBeNull();
  });

  it("resolves contradiction by keeping primary", async () => {
    const provider = createMockDataProviderWithRows(loadMockCandidates());
    const updated = await provider.resolveContradiction(
      "cand_9__cand_16",
      "keep_primary",
      "cand_9",
    );
    expect(updated.contradictionIds).toContain("cand_16");
    const rival = await provider.getCandidate("cand_16");
    expect(rival?.state).toBe("decayed");
    expect(rival?.contradictionIds).toContain("cand_9");
    const keeper = await provider.getCandidate("cand_9");
    expect(keeper).not.toBeNull();
  });

  it("getGraph returns contradiction edge for cand_9 and cand_16", async () => {
    const provider = createMockDataProviderWithRows(loadMockCandidates());
    const graph = await provider.getGraph();
    const hasPair = graph.edges.some(
      (edge) =>
        edge.kind === "contradiction" &&
        ((edge.src === "cand_9" && edge.dst === "cand_16") ||
          (edge.src === "cand_16" && edge.dst === "cand_9")),
    );
    expect(hasPair).toBe(true);
  });

  it("creates, updates, and deletes evals", async () => {
    const provider = createMockDataProviderWithRows(loadMockCandidates());
    const created = await provider.createCandidate({
      title: "Manual eval",
      content: "Always wrap shell pipelines in parentheses.",
      confidence: 0.61,
    });
    expect(created.state).toBe("proposed");
    expect(created.title).toBe("Manual eval");

    const updated = await provider.updateCandidate(created.id, {
      title: "Manual eval (edited)",
      content: created.content,
    });
    expect(updated.title).toBe("Manual eval (edited)");

    await provider.deleteCandidate(created.id);
    expect(await provider.getCandidate(created.id)).toBeNull();
  });
});
