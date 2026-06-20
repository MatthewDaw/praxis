import { describe, expect, it } from "vitest";
import { CONTRACT_HEADER, ORG_HEADER, contractHeaders } from "./contract";

describe("contractHeaders", () => {
  it("sets the contract version header by default", () => {
    const headers = contractHeaders() as Record<string, string>;
    expect(headers[CONTRACT_HEADER]).toBeTruthy();
    expect(headers.Authorization).toBeUndefined();
    expect(headers[ORG_HEADER]).toBeUndefined();
  });

  it("sets Authorization and X-Praxis-Org when token and org are provided", () => {
    const headers = contractHeaders("tok123", "acme") as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer tok123");
    expect(headers[ORG_HEADER]).toBe("acme");
  });

  it("omits X-Praxis-Org when org is absent", () => {
    const headers = contractHeaders("tok123") as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer tok123");
    expect(headers[ORG_HEADER]).toBeUndefined();
  });
});
