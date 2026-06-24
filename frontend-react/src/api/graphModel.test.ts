import { describe, expect, it } from "vitest";
import { candidateFromMapping } from "./candidateModel";
import {
  dedupeEdges,
  deriveGraphFromCandidates,
  mergeGraphWithCandidates,
  parseGraphPayload,
} from "./graphModel";

describe("graphModel", () => {
  it("parses wrapped graph payload", () => {
    const snapshot = parseGraphPayload({
      graph: {
        nodes: [
          {
            id: "cand_1",
            label: "Test",
            state: "proposed",
            confidence: 0.8,
          },
        ],
        edges: [{ src: "cand_1", dst: "cand_2", kind: "support" }],
        scopeGroups: [
          { id: "frontend", label: "Frontend", parentId: null, memberIds: [] },
        ],
      },
    });
    expect(snapshot.nodes).toHaveLength(1);
    expect(snapshot.edges).toHaveLength(1);
    expect(snapshot.scopeGroups).toHaveLength(1);
    expect(snapshot.source).toBe("api");
  });

  it("derives contradiction edges from candidates without duplicates", () => {
    const candidates = [
      candidateFromMapping({
        id: "cand_9",
        title: "A",
        content: "a",
        state: "proposed",
        confidence: 0.7,
        provenance: "logs/test.jsonl:1",
        createdAt: "2026-06-01T00:00:00Z",
        contradiction_ids: ["cand_16"],
      }),
      candidateFromMapping({
        id: "cand_16",
        title: "B",
        content: "b",
        state: "proposed",
        confidence: 0.7,
        provenance: "logs/test.jsonl:2",
        createdAt: "2026-06-01T00:00:00Z",
        contradiction_ids: ["cand_9"],
      }),
    ];
    const graph = deriveGraphFromCandidates(candidates);
    expect(graph.nodes).toHaveLength(2);
    expect(graph.edges).toHaveLength(1);
    expect(graph.edges[0].kind).toBe("contradiction");
    expect(graph.source).toBe("derived");
  });

  it("dedupes bidirectional edges of the same kind", () => {
    const edges = dedupeEdges([
      { src: "a", dst: "b", kind: "contradiction" },
      { src: "b", dst: "a", kind: "contradiction" },
    ]);
    expect(edges).toHaveLength(1);
  });

  it("merges candidate state onto graph nodes", () => {
    const snapshot = parseGraphPayload(
      {
        nodes: [
          {
            id: "cand_1",
            label: "Old title",
            state: "proposed",
            confidence: 0.5,
          },
        ],
        edges: [],
      },
      "mock",
    );
    const candidate = candidateFromMapping({
      id: "cand_1",
      title: "New title",
      content: "body",
      state: "active",
      confidence: 0.9,
      provenance: "logs/test.jsonl:1",
      createdAt: "2026-06-01T00:00:00Z",
      scope: "frontend/react",
      category: "pattern",
    });
    const merged = mergeGraphWithCandidates(snapshot, [candidate]);
    expect(merged.nodes[0].label).toBe("New title");
    expect(merged.nodes[0].state).toBe("active");
    expect(merged.nodes[0].scope).toBe("frontend/react");
  });

  it("removes stale contradiction edges when candidates no longer reference them", () => {
    const snapshot = parseGraphPayload(
      {
        nodes: [
          { id: "cand_9", label: "A", state: "proposed", confidence: 0.7 },
          { id: "cand_16", label: "B", state: "proposed", confidence: 0.7 },
        ],
        edges: [{ src: "cand_9", dst: "cand_16", kind: "contradiction" }],
      },
      "mock",
    );
    const candidates = [
      candidateFromMapping({
        id: "cand_9",
        title: "A",
        content: "a",
        state: "proposed",
        confidence: 0.7,
        provenance: "logs/test.jsonl:1",
        createdAt: "2026-06-01T00:00:00Z",
        contradiction_ids: [],
      }),
      candidateFromMapping({
        id: "cand_16",
        title: "B",
        content: "b",
        state: "rejected",
        confidence: 0.7,
        provenance: "logs/test.jsonl:2",
        createdAt: "2026-06-01T00:00:00Z",
        contradiction_ids: [],
      }),
    ];
    const merged = mergeGraphWithCandidates(snapshot, candidates);
    expect(merged.edges).toHaveLength(0);
  });

  it("adds candidate nodes that are missing from the last graph snapshot", () => {
    const snapshot = parseGraphPayload({ nodes: [], edges: [] }, "mock");
    const candidate = candidateFromMapping({
      id: "cand_new",
      title: "New candidate",
      content: "body",
      state: "proposed",
      confidence: 0.6,
      provenance: "human-gate/manual:1",
      createdAt: "2026-06-01T00:00:00Z",
    });
    const merged = mergeGraphWithCandidates(snapshot, [candidate]);
    expect(merged.nodes).toHaveLength(1);
    expect(merged.nodes[0].id).toBe("cand_new");
  });
});
