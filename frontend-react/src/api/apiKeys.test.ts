import { afterEach, describe, expect, it, vi } from "vitest";
import { createApiKey, listApiKeys, revokeApiKey } from "./apiClient";

const AUTH = { getToken: async () => "token-123", orgId: "monica-demo" };

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("API keys lifecycle", () => {
  it("create → list → revoke round-trips through the documented contract", async () => {
    // In-memory fake backend honoring the apikeys contract.
    const stored: Array<{
      id: string;
      label: string | null;
      userId: string;
      createdAt: string;
      lastUsedAt: string | null;
      revoked: boolean;
    }> = [];

    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string, init: RequestInit) => {
        const path = url.replace("http://127.0.0.1:8000", "");
        const method = init.method ?? "GET";

        if (path === "/apikeys" && method === "POST") {
          const body = JSON.parse(String(init.body)) as { label: string | null };
          const id = `k${stored.length + 1}`;
          stored.push({
            id,
            label: body.label,
            userId: "me",
            createdAt: "2026-06-24T00:00:00Z",
            lastUsedAt: null,
            revoked: false,
          });
          // The raw key is returned ONCE here and never persisted in the list.
          return Promise.resolve(
            new Response(
              JSON.stringify({
                id,
                key: `pxk_secret_${id}`,
                label: body.label,
                createdAt: "2026-06-24T00:00:00Z",
              }),
              { status: 200 },
            ),
          );
        }

        if (path === "/apikeys" && method === "GET") {
          return Promise.resolve(new Response(JSON.stringify(stored), { status: 200 }));
        }

        const revokeMatch = path.match(/^\/apikeys\/(.+)\/revoke$/);
        if (revokeMatch && method === "POST") {
          const target = stored.find((k) => k.id === revokeMatch[1]);
          if (target) target.revoked = true;
          return Promise.resolve(
            new Response(
              JSON.stringify({ id: revokeMatch[1], revoked: true }),
              { status: 200 },
            ),
          );
        }

        return Promise.resolve(new Response("not found", { status: 404 }));
      }),
    );

    // Create: raw pxk_ key returned exactly once.
    const created = await createApiKey("http://127.0.0.1:8000/", "ci-bot", AUTH);
    expect(created.key).toBe("pxk_secret_k1");
    expect(created.label).toBe("ci-bot");

    // List: never exposes the raw key.
    const afterCreate = await listApiKeys("http://127.0.0.1:8000", AUTH);
    expect(afterCreate).toHaveLength(1);
    expect(afterCreate[0]).not.toHaveProperty("key");
    expect(afterCreate[0].revoked).toBe(false);
    expect(afterCreate[0].label).toBe("ci-bot");

    // Revoke flips the active flag.
    const revoked = await revokeApiKey("http://127.0.0.1:8000", created.id, AUTH);
    expect(revoked).toEqual({ id: "k1", revoked: true });

    const afterRevoke = await listApiKeys("http://127.0.0.1:8000", AUTH);
    expect(afterRevoke[0].revoked).toBe(true);
  });

  it("sends a null label and attaches auth + org headers", async () => {
    let requestedUrl = "";
    let body = "";
    let headers: Headers | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string, init: RequestInit) => {
        requestedUrl = url;
        body = String(init.body);
        headers = new Headers(init.headers);
        return Promise.resolve(
          new Response(
            JSON.stringify({ id: "k1", key: "pxk_abc", label: null, createdAt: "t" }),
            { status: 200 },
          ),
        );
      }),
    );

    await createApiKey("http://127.0.0.1:8000", null, AUTH);

    expect(requestedUrl).toBe("http://127.0.0.1:8000/apikeys");
    expect(JSON.parse(body)).toEqual({ label: null });
    expect(headers?.get("Authorization")).toBe("Bearer token-123");
    expect(headers?.get("X-Praxis-Org")).toBe("monica-demo");
  });

  it("tolerates snake_case keys from the backend list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify([
            {
              id: "k9",
              label: null,
              user_id: "ada",
              created_at: "2026-01-01T00:00:00Z",
              last_used_at: "2026-02-02T00:00:00Z",
              revoked: false,
            },
          ]),
          { status: 200 },
        ),
      ),
    );

    const keys = await listApiKeys("http://127.0.0.1:8000");
    expect(keys[0]).toMatchObject({
      id: "k9",
      userId: "ada",
      createdAt: "2026-01-01T00:00:00Z",
      lastUsedAt: "2026-02-02T00:00:00Z",
      revoked: false,
    });
  });
});
