import { afterEach, describe, expect, it, vi } from "vitest";
import { getProductivityStatus } from "./apiClient";

const AUTH = { getToken: async () => "token-123", orgId: "monica-demo" };

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("getProductivityStatus", () => {
  it("GETs /productivity and reports disabled when the kill switch is set", async () => {
    let requestedUrl = "";
    let callCount = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        callCount += 1;
        requestedUrl = url;
        return Promise.resolve(
          new Response(JSON.stringify({ status: "disabled" }), { status: 200 }),
        );
      }),
    );

    const status = await getProductivityStatus("http://127.0.0.1:8000/", AUTH);

    expect(status).toBe("disabled");
    expect(requestedUrl).toBe("http://127.0.0.1:8000/productivity");
    // Exactly one request — the disabled status is served with no follow-up
    // call of any kind (in particular, no GitHub call).
    expect(callCount).toBe(1);
  });

  it("reports enabled when the server says so", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ status: "enabled" }), { status: 200 }),
      ),
    );

    const status = await getProductivityStatus("http://127.0.0.1:8000/", AUTH);
    expect(status).toBe("enabled");
  });
});
