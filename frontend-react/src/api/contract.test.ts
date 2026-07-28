import { describe, expect, it } from "vitest";
import {
  CONTRACT_HEADER,
  ORG_HEADER,
  SPACE_HEADER,
  contractHeaders,
  parseProductivityResponse,
} from "./contract";

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

  it("sets X-Praxis-Space when a space id is provided", () => {
    const headers = contractHeaders("tok123", "acme", "alpha") as Record<string, string>;
    expect(headers[SPACE_HEADER]).toBe("alpha");
  });

  it("omits X-Praxis-Space for the default graph (no space id)", () => {
    const headers = contractHeaders("tok123", "acme") as Record<string, string>;
    expect(headers[SPACE_HEADER]).toBeUndefined();
  });
});

describe("parseProductivityResponse — series_by_org", () => {
  it("parses the per-org S4 breakdown, falling back to the org id when no name is given", () => {
    const parsed = parseProductivityResponse({
      range: "week",
      series_by_org: {
        "org-1": {
          name: "Acme Inc",
          s4_tickets_completed: [{ bucket_start: "2026-07-18T00:00:00+00:00", value: 3 }],
        },
        "org-2": {
          s4_tickets_completed: [{ bucket_start: "2026-07-18T00:00:00+00:00", value: 1 }],
        },
      },
    });

    expect(parsed.seriesByOrg["org-1"]).toEqual({
      name: "Acme Inc",
      ticketsCompleted: [{ bucketStart: "2026-07-18T00:00:00+00:00", value: 3 }],
    });
    expect(parsed.seriesByOrg["org-2"].name).toBe("org-2");
  });

  it("treats an absent series_by_org as an empty breakdown (older backends)", () => {
    expect(parseProductivityResponse({ range: "week" }).seriesByOrg).toEqual({});
  });
});
