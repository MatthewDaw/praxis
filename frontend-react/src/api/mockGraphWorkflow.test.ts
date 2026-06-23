import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { parseGraphPayload } from "./graphModel";
import { createMockDataProviderWithRows } from "./mockProvider";
import { parseCandidateList } from "./candidateModel";

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "../../..");

function loadMockCandidates() {
  const path = join(REPO_ROOT, "frontend-react", "public", "mock-candidates.json");
  return parseCandidateList(JSON.parse(readFileSync(path, "utf-8")));
}

function loadMockGraph() {
  const path = join(REPO_ROOT, "frontend-react", "public", "mock-graph.json");
  return parseGraphPayload(JSON.parse(readFileSync(path, "utf-8")), "mock");
}

describe("mock graph workflow", () => {
  it("loads mock graph with contradiction edge cand_9 cand_16", async () => {
    const provider = createMockDataProviderWithRows(
      loadMockCandidates(),
      loadMockGraph(),
    );
    const graph = await provider.getGraph();
    expect(graph.nodes.length).toBeGreaterThanOrEqual(78);
    const contradiction = graph.edges.find(
      (edge) =>
        edge.kind === "contradiction" &&
        ((edge.src === "cand_9" && edge.dst === "cand_16") ||
          (edge.src === "cand_16" && edge.dst === "cand_9")),
    );
    expect(contradiction).toBeDefined();
    expect(graph.scopeGroups?.length).toBeGreaterThan(0);
  });

  it("promote updates graph node state", async () => {
    const provider = createMockDataProviderWithRows(
      loadMockCandidates(),
      loadMockGraph(),
    );
    await provider.promote("cand_1");
    const graph = await provider.getGraph();
    const node = graph.nodes.find((n) => n.id === "cand_1");
    expect(node?.state).toBe("active");
  });

  it("resolve removes contradiction edge and decays loser", async () => {
    const provider = createMockDataProviderWithRows(
      loadMockCandidates(),
      loadMockGraph(),
    );
    await provider.resolveContradiction(
      "cand_9__cand_16",
      "keep_primary",
      "cand_9",
    );
    const graph = await provider.getGraph();
    const resolvedPair = graph.edges.find(
      (edge) =>
        edge.kind === "contradiction" &&
        ((edge.src === "cand_9" && edge.dst === "cand_16") ||
          (edge.src === "cand_16" && edge.dst === "cand_9")),
    );
    expect(resolvedPair).toBeUndefined();
    const loser = graph.nodes.find((n) => n.id === "cand_16");
    expect(loser?.state).toBe("decayed");
  });
});
