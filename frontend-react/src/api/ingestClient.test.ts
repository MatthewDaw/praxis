import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { postIngestJsonl } from "./apiClient";

const REPO_ROOT = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../..",
);

function loadFixture(name: string): unknown {
  const path = join(REPO_ROOT, "docs", "integration", "fixtures", name);
  return JSON.parse(readFileSync(path, "utf-8"));
}

describe("postIngestJsonl", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("throws a friendly message when the ingest endpoint is missing", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("Not Found", { status: 404, statusText: "Not Found" }),
      ),
    );

    const payload = loadFixture("ingest-jsonl-request.json") as {
      files: Array<{ name: string; content: string }>;
    };

    await expect(
      postIngestJsonl("http://127.0.0.1:8000", payload.files),
    ).rejects.toThrow("Distillation endpoint not available yet");
  });

  it("accepts a successful ingest response without parsing a body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("", { status: 200 })),
    );

    const payload = loadFixture("ingest-jsonl-request.json") as {
      files: Array<{ name: string; content: string }>;
    };

    await expect(
      postIngestJsonl("http://127.0.0.1:8000", payload.files),
    ).resolves.toBeUndefined();
  });
});
