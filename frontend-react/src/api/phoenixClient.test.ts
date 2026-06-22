import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchPhoenixTraces,
  hasPhoenixLink,
  phoenixLinkFromExtra,
  PhoenixUnconfiguredError,
} from "./phoenixClient";

const MOCK_FIXTURE = {
  project: "praxis-eval",
  phoenixBaseUrl: "https://phoenix.example.com",
  traces: [
    {
      traceId: "trace-a",
      sessionId: "session-1",
      startTime: "2026-06-15T14:00:00Z",
      latencyMs: 1200,
      statusCode: "OK",
      spanCount: 2,
      tokens: { prompt: 100, completion: 50, total: 150 },
      model: "claude-sonnet",
      spans: [],
      phoenixUrl:
        "https://phoenix.example.com/v1/projects/praxis-eval/spans?trace_id=trace-a",
    },
    {
      traceId: "trace-b",
      sessionId: "session-2",
      startTime: null,
      latencyMs: null,
      statusCode: "ERROR",
      spanCount: 1,
      tokens: { prompt: null, completion: null, total: null },
      model: null,
      spans: [],
      phoenixUrl: null,
    },
  ],
};

function stubFetch(impl: (url: string) => Response) {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => Promise.resolve(impl(String(input)))),
  );
}

describe("phoenixLinkFromExtra", () => {
  it("reads camelCase, snake_case, and prefixed identifiers", () => {
    expect(phoenixLinkFromExtra({ traceId: "t1" }).traceId).toBe("t1");
    expect(phoenixLinkFromExtra({ trace_id: "t2" }).traceId).toBe("t2");
    expect(phoenixLinkFromExtra({ phoenixTraceId: "t3" }).traceId).toBe("t3");
    expect(phoenixLinkFromExtra({ session_id: "s1" }).sessionId).toBe("s1");
    expect(phoenixLinkFromExtra({ phoenixProject: "p1" }).project).toBe("p1");
  });

  it("ignores blank values", () => {
    const link = phoenixLinkFromExtra({ traceId: "   ", sessionId: "" });
    expect(hasPhoenixLink(link)).toBe(false);
  });
});

describe("fetchPhoenixTraces (mock/local modes)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("returns empty without a Phoenix link and never fetches", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const result = await fetchPhoenixTraces({}, "mock");
    expect(result.traces).toEqual([]);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("filters the fixture by traceId", async () => {
    stubFetch(() => new Response(JSON.stringify(MOCK_FIXTURE), { status: 200 }));
    const result = await fetchPhoenixTraces({ traceId: "trace-a" }, "mock");
    expect(result.traces).toHaveLength(1);
    expect(result.traces[0].traceId).toBe("trace-a");
  });

  it("filters the fixture by sessionId in local-logs mode", async () => {
    stubFetch(() => new Response(JSON.stringify(MOCK_FIXTURE), { status: 200 }));
    const result = await fetchPhoenixTraces({ sessionId: "session-2" }, "local-logs");
    expect(result.traces).toHaveLength(1);
    expect(result.traces[0].traceId).toBe("trace-b");
  });

  it("returns no traces when the id does not match", async () => {
    stubFetch(() => new Response(JSON.stringify(MOCK_FIXTURE), { status: 200 }));
    const result = await fetchPhoenixTraces({ traceId: "missing" }, "mock");
    expect(result.traces).toEqual([]);
  });
});

describe("fetchPhoenixTraces (live mode via proxy)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("calls the proxy with the trace_id query and returns normalized traces", async () => {
    vi.stubEnv("VITE_PRAXIS_PHOENIX_PROXY_URL", "http://localhost:8800");
    let requestedUrl = "";
    stubFetch((url) => {
      requestedUrl = url;
      return new Response(
        JSON.stringify({
          project: "praxis-eval",
          phoenixBaseUrl: "https://phoenix.example.com",
          traces: [MOCK_FIXTURE.traces[0]],
        }),
        { status: 200 },
      );
    });

    const result = await fetchPhoenixTraces({ traceId: "trace-a" }, "live");
    expect(requestedUrl).toContain("http://localhost:8800/phoenix/traces");
    expect(requestedUrl).toContain("trace_id=trace-a");
    expect(result.traces[0].traceId).toBe("trace-a");
  });

  it("raises PhoenixUnconfiguredError when the proxy URL is unset", async () => {
    vi.stubEnv("VITE_PRAXIS_PHOENIX_PROXY_URL", "");
    await expect(
      fetchPhoenixTraces({ traceId: "trace-a" }, "live"),
    ).rejects.toBeInstanceOf(PhoenixUnconfiguredError);
  });

  it("raises PhoenixUnconfiguredError on a 503 from the proxy", async () => {
    vi.stubEnv("VITE_PRAXIS_PHOENIX_PROXY_URL", "http://localhost:8800");
    stubFetch(() => new Response("unconfigured", { status: 503 }));
    await expect(
      fetchPhoenixTraces({ traceId: "trace-a" }, "live"),
    ).rejects.toBeInstanceOf(PhoenixUnconfiguredError);
  });

  it("throws on other proxy errors", async () => {
    vi.stubEnv("VITE_PRAXIS_PHOENIX_PROXY_URL", "http://localhost:8800");
    stubFetch(() => new Response("boom", { status: 502 }));
    await expect(
      fetchPhoenixTraces({ traceId: "trace-a" }, "live"),
    ).rejects.toThrow("502");
  });
});
