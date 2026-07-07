import { afterEach, describe, expect, it, vi } from "vitest";
import { foldIn, getSnapshotFacts, listOrgSources, listSnapshots } from "./apiClient";

const AUTH = { getToken: async () => "token-123", orgId: "monica-demo" };
const SPACE_AUTH = { ...AUTH, spaceId: "build-plan" };

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("listOrgSources", () => {
  it("GETs /org/sources with org auth and normalizes the sources", async () => {
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
              sources: [
                { userId: "me", role: "owner", isSelf: true, snapshots: ["v1"] },
                { userId: "ada", role: "member", isSelf: false, snapshots: [] },
              ],
            }),
            { status: 200 },
          ),
        );
      }),
    );

    const sources = await listOrgSources("http://127.0.0.1:8000/", AUTH);

    expect(requestedUrl).toBe("http://127.0.0.1:8000/org/sources");
    expect(headers?.get("Authorization")).toBe("Bearer token-123");
    expect(headers?.get("X-Praxis-Org")).toBe("monica-demo");
    expect(sources).toEqual([
      {
        userId: "me",
        username: null,
        role: "owner",
        isSelf: true,
        snapshots: [{ name: "v1", count: 0 }],
      },
      { userId: "ada", username: null, role: "member", isSelf: false, snapshots: [] },
    ]);
  });

  it("tolerates snake_case keys from the backend", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            sources: [{ user_id: "ada", role: "member", is_self: false, snapshots: [] }],
          }),
          { status: 200 },
        ),
      ),
    );

    const sources = await listOrgSources("http://127.0.0.1:8000");
    expect(sources[0]).toMatchObject({ userId: "ada", isSelf: false });
  });
});

describe("listSnapshots", () => {
  it("maps the backend `snapshot` key onto each snapshot's name", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            space: "build-plan",
            snapshots: [
              { snapshot: "prd-build-plan", count: 44, createdAt: "2026-07-07" },
            ],
          }),
          { status: 200 },
        ),
      ),
    );

    const snapshots = await listSnapshots("http://127.0.0.1:8000", SPACE_AUTH);

    // Regression: the backend returns the name under `snapshot`, not `name`.
    // Reading the wrong key left every snapshot nameless — it rendered as
    // "(44 nodes)" and its empty <option value> could not be selected.
    expect(snapshots).toEqual([
      { name: "prd-build-plan", count: 44, createdAt: "2026-07-07" },
    ]);
  });
});

describe("getSnapshotFacts", () => {
  it("GETs the snapshot facts endpoint and normalizes folder groups", async () => {
    let requestedUrl = "";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        requestedUrl = url;
        return Promise.resolve(
          new Response(
            JSON.stringify({
              userId: "ada",
              snapshot: "v1",
              groups: [
                {
                  key: "g1",
                  label: "Testing",
                  facts: [
                    {
                      id: "f1",
                      text: "Write tests first",
                      scope: "global",
                      clusterLabel: "Testing",
                      state: "active",
                    },
                  ],
                },
              ],
            }),
            { status: 200 },
          ),
        );
      }),
    );

    const facts = await getSnapshotFacts("http://127.0.0.1:8000", "ada", "v1");

    expect(requestedUrl).toBe(
      "http://127.0.0.1:8000/org/sources/ada/snapshots/v1/facts",
    );
    expect(facts.snapshot).toBe("v1");
    expect(facts.groups).toHaveLength(1);
    expect(facts.groups[0].facts[0].text).toBe("Write tests first");
  });
});

describe("foldIn", () => {
  it("POSTs sourceUser/snapshot/factIds/mode and normalizes the result", async () => {
    let requestedUrl = "";
    let requestedBody = "";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string, init: RequestInit) => {
        requestedUrl = url;
        requestedBody = String(init.body);
        return Promise.resolve(
          new Response(
            JSON.stringify({
              folded: 3,
              deduped: 1,
              conflicts: [{ newId: "n1", rivalId: "r1" }],
              mode: "add",
            }),
            { status: 200 },
          ),
        );
      }),
    );

    const result = await foldIn(
      "http://127.0.0.1:8000/",
      "ada",
      "v1",
      ["f1", "f2"],
      "add",
      AUTH,
    );

    expect(requestedUrl).toBe("http://127.0.0.1:8000/fold-in");
    expect(JSON.parse(requestedBody)).toEqual({
      sourceUser: "ada",
      snapshot: "v1",
      factIds: ["f1", "f2"],
      mode: "add",
    });
    expect(result).toEqual({
      folded: 3,
      deduped: 1,
      conflicts: [{ newId: "n1", rivalId: "r1" }],
      mode: "add",
    });
  });

  it("sends mode=replace when replacing the graph", async () => {
    let requestedBody = "";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((_url: string, init: RequestInit) => {
        requestedBody = String(init.body);
        return Promise.resolve(
          new Response(
            JSON.stringify({ folded: 2, deduped: 0, conflicts: [], mode: "replace" }),
            { status: 200 },
          ),
        );
      }),
    );

    const result = await foldIn(
      "http://127.0.0.1:8000",
      "ada",
      "v1",
      ["f1"],
      "replace",
    );

    expect(JSON.parse(requestedBody).mode).toBe("replace");
    expect(result.mode).toBe("replace");
  });

  it("normalizes snake_case conflict ids and defaults mode to add", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            folded: 0,
            deduped: 0,
            conflicts: [{ new_id: "n9", rival_id: "r9" }],
          }),
          { status: 200 },
        ),
      ),
    );

    const result = await foldIn("http://127.0.0.1:8000", "ada", "v1", ["f1"], "add");
    expect(result.conflicts).toEqual([{ newId: "n9", rivalId: "r9" }]);
    expect(result.mode).toBe("add");
  });
});
