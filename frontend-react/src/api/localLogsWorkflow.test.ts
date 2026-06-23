import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  createLocalLogsDataProviderFromFiles,
} from "./localLogsProvider";

const FIXTURE = join(
  dirname(fileURLToPath(import.meta.url)),
  "fixtures",
  "sample-transcript.jsonl",
);

describe("local logs workflow", () => {
  const files = [
    {
      name: "sample-transcript.jsonl",
      content: readFileSync(FIXTURE, "utf-8"),
    },
  ];

  it("lists heuristic candidates from uploaded fixture", async () => {
    const provider = createLocalLogsDataProviderFromFiles(files);
    const candidates = await provider.listCandidates();
    expect(candidates.length).toBeGreaterThanOrEqual(1);
  });

  it("returns transcript session via getTranscript", async () => {
    const provider = createLocalLogsDataProviderFromFiles(files);
    const session = await provider.getTranscript();
    expect(session).not.toBeNull();
    expect(session?.lines.length).toBeGreaterThan(0);
    expect(session?.files[0].name).toBe("sample-transcript.jsonl");
  });

  it("promote updates candidate and graph state", async () => {
    const provider = createLocalLogsDataProviderFromFiles(files);
    const candidates = await provider.listCandidates();
    const first = candidates[0];
    const updated = await provider.promote(first.id);
    expect(updated.state).toBe("active");
    const graph = await provider.getGraph();
    const node = graph.nodes.find((n) => n.id === first.id);
    expect(node?.state).toBe("active");
  });
});
