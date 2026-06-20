import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("aws-amplify/auth", () => ({
  fetchAuthSession: vi.fn(async () => ({ tokens: { idToken: { toString: () => "id-token" } } })),
  signOut: vi.fn(async () => {}),
}));

import { createOrg, fetchMe, joinOrg } from "./OrgGate";

const BASE = "http://api.test";
const getToken = async () => "id-token";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("OrgGate API helpers", () => {
  it("fetchMe sends the bearer token and normalizes memberships", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({
        sub: "user-1",
        email: "a@b.co",
        orgs: [{ org_id: "acme", name: "Acme", role: "owner" }],
      }),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const me = await fetchMe(BASE, getToken);

    expect(me.sub).toBe("user-1");
    expect(me.orgs).toEqual([{ orgId: "acme", name: "Acme", role: "owner" }]);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://api.test/me");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer id-token");
  });

  it("createOrg POSTs the create body", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, text: async () => "" }));
    vi.stubGlobal("fetch", fetchMock);

    await createOrg(BASE, getToken, { orgId: "acme", name: "Acme", password: "pw" });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://api.test/orgs");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      orgId: "acme",
      name: "Acme",
      password: "pw",
    });
  });

  it("joinOrg POSTs the join body to /orgs/join", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, text: async () => "" }));
    vi.stubGlobal("fetch", fetchMock);

    await joinOrg(BASE, getToken, { orgId: "acme", password: "pw" });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://api.test/orgs/join");
    expect(JSON.parse(init.body as string)).toEqual({ orgId: "acme", password: "pw" });
  });

  it("createOrg surfaces the server error detail", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 409,
      text: async () => "org exists",
    }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createOrg(BASE, getToken, { orgId: "acme", name: "Acme", password: "pw" }),
    ).rejects.toThrow("org exists");
  });
});
