import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  EvalRegenerateUnavailableError,
  GraphIngestUnavailableError,
  postIngestJsonl,
  postInsight,
  postRegenerateEvals,
} from "./apiClient";

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

describe("postInsight", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts active insight text to the graph ingest endpoint with org auth", async () => {
    let requestedUrl = "";
    let requestedBody = "";
    let requestedHeaders: Headers | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string, init: RequestInit) => {
        requestedUrl = url;
        requestedBody = String(init.body);
        requestedHeaders = new Headers(init.headers);
        return Promise.resolve(
          new Response(
            JSON.stringify({
              summary: "added insight",
              action: "added",
              id: "fact-123",
            }),
            { status: 200 },
          ),
        );
      }),
    );

    const result = await postInsight(
      "http://127.0.0.1:8000/",
      "  Prefer the eval ingest spine.  ",
      {
        getToken: async () => "token-123",
        orgId: "monica-demo",
      },
    );

    expect(requestedUrl).toBe("http://127.0.0.1:8000/insights");
    expect(JSON.parse(requestedBody)).toEqual({
      insight: "Prefer the eval ingest spine.",
    });
    expect(requestedHeaders?.get("Authorization")).toBe("Bearer token-123");
    expect(requestedHeaders?.get("X-Praxis-Org")).toBe("monica-demo");
    expect(result).toEqual({
      summary: "added insight",
      action: "added",
      id: "fact-123",
    });
  });

  it("reports a non-blocking unavailable error when graph ingest has no database", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "insights require a database" }), {
          status: 503,
          statusText: "Service Unavailable",
        }),
      ),
    );

    const result = postInsight("http://127.0.0.1:8000", "Approved lesson");
    await expect(result).rejects.toBeInstanceOf(GraphIngestUnavailableError);
    await expect(result).rejects.toThrow("insights require a database");
  });
});

describe("postRegenerateEvals", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts the selected eval preset with org auth", async () => {
    let requestedUrl = "";
    let requestedBody = "";
    let requestedHeaders: Headers | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string, init: RequestInit) => {
        requestedUrl = url;
        requestedBody = String(init.body);
        requestedHeaders = new Headers(init.headers);
        return Promise.resolve(
          new Response(
            JSON.stringify({
              preset: "offline-fake",
              cases_run: 5,
              cases_skipped: 0,
              insights_generated: 7,
              candidates_inserted: 7,
              ran_at: "2026-06-22T12:00:00Z",
            }),
            { status: 200 },
          ),
        );
      }),
    );

    const result = await postRegenerateEvals(
      "http://127.0.0.1:8000/",
      "offline-fake",
      {
        getToken: async () => "token-123",
        orgId: "monica-demo",
      },
    );

    expect(requestedUrl).toBe("http://127.0.0.1:8000/evals/regenerate");
    expect(JSON.parse(requestedBody)).toEqual({ preset: "offline-fake" });
    expect(requestedHeaders?.get("Authorization")).toBe("Bearer token-123");
    expect(requestedHeaders?.get("X-Praxis-Org")).toBe("monica-demo");
    expect(result).toEqual({
      preset: "offline-fake",
      casesRun: 5,
      casesSkipped: 0,
      insightsGenerated: 7,
      candidatesInserted: 7,
      ranAt: "2026-06-22T12:00:00Z",
    });
  });

  it("reports an unavailable error when the regenerate endpoint is missing", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("Not Found", { status: 404, statusText: "Not Found" }),
      ),
    );

    const result = postRegenerateEvals("http://127.0.0.1:8000", "offline-fake");
    await expect(result).rejects.toBeInstanceOf(EvalRegenerateUnavailableError);
  });
});
