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
  parseProductivityResponse,
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
    expect(candidates[1].state).toBe("active");
    expect(candidates[2].contradictionIds).toEqual(["cand_16"]);
  });

  it("parses rich contradictions into pending/resolved links", () => {
    const candidate = candidateFromMapping({
      id: "cand_9",
      title: "Rich",
      contradictions: [
        { id: "cand_16", status: "pending" },
        { id: "cand_22", status: "resolved" },
      ],
    });
    expect(candidate.contradictionIds).toEqual(["cand_16", "cand_22"]);
    expect(candidate.contradictions).toEqual([
      { id: "cand_16", status: "pending" },
      { id: "cand_22", status: "resolved" },
    ]);
  });

  it("synthesizes pending links when only contradiction_ids are present", () => {
    const candidate = candidateFromMapping({
      id: "cand_9",
      title: "Flat",
      contradiction_ids: ["cand_16"],
    });
    expect(candidate.contradictions).toEqual([
      { id: "cand_16", status: "pending" },
    ]);
  });

  it("promotes proposed directly to active", () => {
    expect(buildPromoteBody("proposed")).toEqual({ targetState: "active" });
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

  it("validates ingest-jsonl-request.json shape", () => {
    const payload = loadFixture("ingest-jsonl-request.json") as {
      files: Array<{ name: string; content: string }>;
    };
    expect(Array.isArray(payload.files)).toBe(true);
    expect(payload.files.length).toBeGreaterThanOrEqual(1);
    expect(payload.files[0].name).toBeTruthy();
    expect(typeof payload.files[0].content).toBe("string");
  });

  it("parses productivity-response.json into the typed contract shape", () => {
    const payload = loadFixture("productivity-response.json");
    const parsed = parseProductivityResponse(payload);
    expect(parsed.range).toBe("week");
    expect(parsed.truncated).toBe(false);
    expect(parsed.series.linesAdded).toEqual([
      { bucketStart: "2026-07-18T00:00:00+00:00", value: 120 },
    ]);
    expect(parsed.series.linesDeleted[0].value).toBe(40);
    expect(parsed.series.netLines[0].value).toBe(80);
    expect(parsed.series.ticketsCompleted[0].value).toBe(3);
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
