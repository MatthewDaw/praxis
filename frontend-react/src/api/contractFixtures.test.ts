import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  candidateFromMapping,
  parseCandidateList,
} from "./candidateModel";
import {
  buildPromoteBody,
  buildResolveBody,
  contradictionPairId,
  normalizeResolution,
} from "./contract";

const REPO_ROOT = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../..",
);

function loadFixture(name: string): unknown {
  const path = join(REPO_ROOT, "docs", "integration", "fixtures", name);
  return JSON.parse(readFileSync(path, "utf-8"));
}

describe("contract v1 fixtures", () => {
  it("parses candidates-list.json into models", () => {
    const payload = loadFixture("candidates-list.json");
    const rows = parseCandidateList(payload);
    expect(rows).toHaveLength(3);
    const candidates = rows.map(candidateFromMapping);
    expect(candidates[0].id).toBe("cand_1");
    expect(candidates[0].state).toBe("proposed");
    expect(candidates[1].state).toBe("suggested");
    expect(candidates[2].contradictionIds).toEqual(["cand_16"]);
  });

  it("matches promote-request.json builder", () => {
    const expected = loadFixture("promote-request.json");
    expect(buildPromoteBody("proposed")).toEqual(expected);
  });

  it("matches resolve-request.json builder", () => {
    const expected = loadFixture("resolve-request.json");
    expect(
      buildResolveBody("keep_primary", "cand_9"),
    ).toEqual(expected);
  });

  it("maps UI resolution labels to API values", () => {
    expect(normalizeResolution("keep_primary")).toBe("keep_a");
    expect(normalizeResolution("keep_rival")).toBe("keep_b");
  });

  it("formats contradiction pair ids", () => {
    expect(contradictionPairId("cand_9", "cand_16")).toBe("cand_9__cand_16");
  });

  it("parses wrapped candidate list shape", () => {
    const rows = parseCandidateList({
      candidates: [{ id: "x", title: "t" }],
    });
    expect(rows).toHaveLength(1);
    expect(rows[0].id).toBe("x");
  });

  it("requires curve fields in eval-metrics.json", () => {
    const metrics = loadFixture("eval-metrics.json") as Record<string, unknown>;
    const series = metrics.correction_rate;
    expect(Array.isArray(series)).toBe(true);
    expect((series as unknown[]).length).toBeGreaterThanOrEqual(2);
    expect(metrics.corrections_before).toBeDefined();
    expect(metrics.corrections_after).toBeDefined();
  });

  it("validates ingest-jsonl-request.json shape", () => {
    const payload = loadFixture("ingest-jsonl-request.json") as {
      files: Array<{ name: string; content: string }>;
    };
    expect(Array.isArray(payload.files)).toBe(true);
    expect(payload.files.length).toBeGreaterThanOrEqual(1);
    expect(payload.files[0].name).toBeTruthy();
    expect(typeof payload.files[0].content).toBe("string");
  });

  it("validates ingest-jsonl-response.json shape", () => {
    const payload = loadFixture("ingest-jsonl-response.json") as {
      candidatesCreated: number;
      candidateIds: string[];
      provenance: string[];
    };
    expect(typeof payload.candidatesCreated).toBe("number");
    expect(Array.isArray(payload.candidateIds)).toBe(true);
    expect(Array.isArray(payload.provenance)).toBe(true);
  });
});
