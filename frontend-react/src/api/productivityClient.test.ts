import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { isProductivityDisabled } from "./productivityClient";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("isProductivityDisabled (R39 kill-switch signal)", () => {
  it("returns true when the backend reports a disabled status", async () => {
    globalThis.fetch = vi.fn(async () => ({
      ok: true,
      json: async () => ({ status: "disabled" }),
    })) as unknown as typeof fetch;
    await expect(isProductivityDisabled("https://api.example")).resolves.toBe(true);
  });

  it("returns false when the backend returns live series data", async () => {
    globalThis.fetch = vi.fn(async () => ({
      ok: true,
      json: async () => ({ range: "week", series: {} }),
    })) as unknown as typeof fetch;
    await expect(isProductivityDisabled("https://api.example")).resolves.toBe(false);
  });

  it("returns false (fail open on tab visibility) on a non-ok response", async () => {
    globalThis.fetch = vi.fn(async () => ({
      ok: false,
      status: 403,
      text: async () => "not the productivity token owner",
    })) as unknown as typeof fetch;
    await expect(isProductivityDisabled("https://api.example")).resolves.toBe(false);
  });

  it("returns false on a network error rather than throwing", async () => {
    globalThis.fetch = vi.fn(async () => {
      throw new Error("network down");
    }) as unknown as typeof fetch;
    await expect(isProductivityDisabled("https://api.example")).resolves.toBe(false);
  });
});
