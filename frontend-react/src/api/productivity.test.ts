import { afterEach, describe, expect, it, vi } from "vitest";
import { getProductivity } from "./apiClient";

const AUTH = { getToken: async () => "token-123", orgId: "monica-demo", spaceId: "alpha" };

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("getProductivity", () => {
  it("fetches GET /productivity with the range query param and contract headers (bearer, org, space)", async () => {
    let requestedUrl = "";
    let method = "";
    let headers: Headers | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string, init: RequestInit) => {
        requestedUrl = url;
        method = init.method ?? "GET";
        headers = new Headers(init.headers);
        return Promise.resolve(
          new Response(
            JSON.stringify({
              range: "week",
              truncated: false,
              series: {
                s1_lines_added: [{ bucket_start: "2026-07-18T00:00:00+00:00", value: 120 }],
                s2_lines_deleted: [{ bucket_start: "2026-07-18T00:00:00+00:00", value: 40 }],
                s3_net_lines: [{ bucket_start: "2026-07-18T00:00:00+00:00", value: 80 }],
                s4_tickets_completed: [{ bucket_start: "2026-07-18T00:00:00+00:00", value: 3 }],
              },
            }),
            { status: 200 },
          ),
        );
      }),
    );

    const result = await getProductivity("http://127.0.0.1:8000", "week", AUTH);

    expect(requestedUrl).toBe("http://127.0.0.1:8000/productivity?range=week");
    expect(method).toBe("GET");
    expect(headers?.get("Authorization")).toBe("Bearer token-123");
    expect(headers?.get("X-Praxis-Org")).toBe("monica-demo");
    expect(headers?.get("X-Praxis-Space")).toBe("alpha");

    expect(result.range).toBe("week");
    expect(result.truncated).toBe(false);
    expect(result.series.linesAdded).toEqual([
      { bucketStart: "2026-07-18T00:00:00+00:00", value: 120 },
    ]);
    expect(result.series.linesDeleted[0].value).toBe(40);
    expect(result.series.netLines[0].value).toBe(80);
    expect(result.series.ticketsCompleted[0].value).toBe(3);
  });

  it("throws an ApiClientError on a non-2xx response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("not the owner", { status: 403 })),
    );

    await expect(getProductivity("http://127.0.0.1:8000", "day", AUTH)).rejects.toThrow(
      /403/,
    );
  });

  it("encodes the range query param for every allowed range", async () => {
    const seen: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        seen.push(url);
        return Promise.resolve(
          new Response(JSON.stringify({ range: "day", truncated: false, series: {} }), {
            status: 200,
          }),
        );
      }),
    );

    for (const range of ["day", "week", "4weeks", "12months", "alltime"] as const) {
      await getProductivity("http://127.0.0.1:8000", range, AUTH);
    }

    expect(seen).toEqual([
      "http://127.0.0.1:8000/productivity?range=day",
      "http://127.0.0.1:8000/productivity?range=week",
      "http://127.0.0.1:8000/productivity?range=4weeks",
      "http://127.0.0.1:8000/productivity?range=12months",
      "http://127.0.0.1:8000/productivity?range=alltime",
    ]);
  });
});
