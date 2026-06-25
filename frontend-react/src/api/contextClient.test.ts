import { afterEach, describe, expect, it, vi } from "vitest";
import {
  buildContextQueryString,
  contextHitFromWire,
  factDependentFromWire,
  factUtility,
  formatUtility,
  getContext,
  getFactDependents,
  getStaleDerivations,
  parseFactTrust,
  recordFactOutcome,
  staleDerivationFromWire,
  utilityTone,
} from "./contextClient";

describe("factUtility (H1 Laplace-smoothed trust)", () => {
  it("is neutral 1.0 with no recorded outcomes", () => {
    expect(factUtility(0, 0)).toBe(1.0);
    expect(factUtility()).toBe(1.0);
  });

  it("applies (success + 0.5) / (total + 1) once outcomes exist", () => {
    expect(factUtility(1, 0)).toBeCloseTo(0.75, 5); // (1.5)/(2)
    expect(factUtility(0, 1)).toBeCloseTo(0.25, 5); // (0.5)/(2)
    expect(factUtility(3, 1)).toBeCloseTo(0.7, 5); // (3.5)/(5)
  });
});

describe("utilityTone / formatUtility", () => {
  it("classifies no-evidence as neutral", () => {
    expect(utilityTone(1.0, 0, 0)).toBe("neutral");
  });
  it("classifies high utility as trusted and low as risky", () => {
    expect(utilityTone(factUtility(5, 0), 5, 0)).toBe("trusted");
    expect(utilityTone(factUtility(0, 5), 0, 5)).toBe("risky");
  });
  it("formats to two decimals", () => {
    expect(formatUtility(0.75)).toBe("0.75");
  });
});

describe("parseFactTrust", () => {
  it("returns undefined when no trust fields present", () => {
    expect(parseFactTrust({ id: "f1" })).toBeUndefined();
  });
  it("reads snake_case counts and derives utility when absent", () => {
    expect(parseFactTrust({ success_count: 1, failure_count: 0 })).toEqual({
      successCount: 1,
      failureCount: 0,
      utility: 0.75,
    });
  });
  it("prefers a server-supplied utility", () => {
    expect(
      parseFactTrust({ successCount: 2, failureCount: 2, utility: 0.42 }),
    ).toEqual({ successCount: 2, failureCount: 2, utility: 0.42 });
  });
});

describe("contextHitFromWire", () => {
  it("maps source/scope/category, trust, and meta", () => {
    const hit = contextHitFromWire({
      id: "f1",
      text: "Prefer the eval ingest spine.",
      source: "session-12",
      scope: "praxis/api",
      category: "semantic",
      success_count: 3,
      failure_count: 1,
      meta: { author: "agent", confidence: 0.9 },
    });
    expect(hit).toMatchObject({
      id: "f1",
      source: "session-12",
      scope: "praxis/api",
      category: "semantic",
      meta: { author: "agent", confidence: 0.9 },
    });
    expect(hit.trust?.utility).toBeCloseTo(0.7, 5);
  });
  it("omits meta when empty", () => {
    expect(contextHitFromWire({ id: "f1", meta: {} }).meta).toBeUndefined();
    expect(contextHitFromWire({ id: "f1" }).meta).toBeUndefined();
  });
});

describe("buildContextQueryString (param wiring)", () => {
  it("includes query and top_k but omits as_of and defaults include_episodic off", () => {
    expect(buildContextQueryString({ query: "deploy", topK: 5 })).toBe(
      "query=deploy&top_k=5",
    );
  });
  it("threads as_of when pinned", () => {
    const qs = buildContextQueryString({
      query: "deploy",
      asOf: "2026-06-01T00:00:00Z",
    });
    expect(qs).toContain("as_of=2026-06-01T00%3A00%3A00Z");
  });
  it("sends include_episodic=true only when opted in", () => {
    expect(buildContextQueryString({ query: "x", includeEpisodic: true })).toBe(
      "query=x&include_episodic=true",
    );
    expect(buildContextQueryString({ query: "x", includeEpisodic: false })).toBe(
      "query=x",
    );
  });
});

describe("getContext fetch wiring", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("GETs /context with threaded params and org auth, maps hits", async () => {
    let requestedUrl = "";
    let headers: Headers | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string, init: RequestInit) => {
        requestedUrl = url;
        headers = new Headers(init.headers);
        return Promise.resolve(
          new Response(
            JSON.stringify({
              hits: [
                { id: "f1", text: "a", source: "s", scope: "sc", category: "semantic" },
              ],
            }),
            { status: 200 },
          ),
        );
      }),
    );

    const hits = await getContext(
      "http://127.0.0.1:8000/",
      { query: "deploy", topK: 3, asOf: "2026-06-01T00:00:00Z", includeEpisodic: true },
      { getToken: async () => "tok", orgId: "monica-demo" },
    );

    expect(requestedUrl).toBe(
      "http://127.0.0.1:8000/context?query=deploy&top_k=3&as_of=2026-06-01T00%3A00%3A00Z&include_episodic=true",
    );
    expect(headers?.get("Authorization")).toBe("Bearer tok");
    expect(headers?.get("X-Praxis-Org")).toBe("monica-demo");
    expect(hits).toHaveLength(1);
    expect(hits[0].id).toBe("f1");
  });
});

describe("recordFactOutcome", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("POSTs the outcome and returns updated trust", async () => {
    let requestedUrl = "";
    let body = "";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string, init: RequestInit) => {
        requestedUrl = url;
        body = String(init.body);
        return Promise.resolve(
          new Response(
            JSON.stringify({ success_count: 2, failure_count: 1 }),
            { status: 200 },
          ),
        );
      }),
    );

    const trust = await recordFactOutcome("http://127.0.0.1:8000", "f1", true);
    expect(requestedUrl).toBe("http://127.0.0.1:8000/facts/f1/outcome");
    expect(JSON.parse(body)).toEqual({ success: true });
    expect(trust).toEqual({ successCount: 2, failureCount: 1, utility: factUtility(2, 1) });
  });

  it("throws on a non-ok response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("nope", { status: 404 })),
    );
    await expect(
      recordFactOutcome("http://127.0.0.1:8000", "f1", false),
    ).rejects.toThrow(/failed \(404\)/);
  });
});

describe("derivation consumer mappings (H5)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("maps dependents incl stale flag", () => {
    expect(
      factDependentFromWire({ id: "d1", text: "downstream", scope: "x", is_stale: true }),
    ).toEqual({ id: "d1", text: "downstream", scope: "x", stale: true });
  });

  it("maps stale derivations with source ids", () => {
    expect(
      staleDerivationFromWire({ id: "d1", text: "t", source_ids: ["s1", "s2"] }),
    ).toEqual({ id: "d1", text: "t", sourceIds: ["s1", "s2"] });
  });

  it("GETs /facts/{id}/dependents and maps the list", async () => {
    let url = "";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((u: string) => {
        url = u;
        return Promise.resolve(
          new Response(JSON.stringify({ dependents: [{ id: "d1", stale: false }] }), {
            status: 200,
          }),
        );
      }),
    );
    const deps = await getFactDependents("http://127.0.0.1:8000", "f1");
    expect(url).toBe("http://127.0.0.1:8000/facts/f1/dependents");
    expect(deps).toEqual([{ id: "d1", text: "", scope: "", stale: false }]);
  });

  it("GETs /derivations/stale and maps the list", async () => {
    let url = "";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((u: string) => {
        url = u;
        return Promise.resolve(
          new Response(JSON.stringify({ stale: [{ id: "d1", text: "t", sourceIds: ["s1"] }] }), {
            status: 200,
          }),
        );
      }),
    );
    const stale = await getStaleDerivations("http://127.0.0.1:8000");
    expect(url).toBe("http://127.0.0.1:8000/derivations/stale");
    expect(stale).toEqual([{ id: "d1", text: "t", sourceIds: ["s1"] }]);
  });
});
