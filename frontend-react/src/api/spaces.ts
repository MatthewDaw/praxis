import { contractHeaders } from "./contract";

/**
 * A space is a login's private, named working knowledge graph within an org.
 * Selecting one sends `X-Praxis-Space` so reads/writes target a sibling graph
 * (the backend derives `user_id = principal.sub::space:<id>`); the default
 * space sends no header and targets the login's base graph.
 */
export interface Space {
  spaceId: string;
  name?: string;
  createdAt?: string;
}

function normalizeSpace(payload: unknown): Space | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }
  const row = payload as Record<string, unknown>;
  const spaceId = (row.spaceId ?? row.space_id) as string | undefined;
  if (!spaceId) {
    return null;
  }
  const createdAt = (row.createdAt ?? row.created_at) as string | undefined;
  return {
    spaceId,
    name: typeof row.name === "string" ? row.name : undefined,
    createdAt: typeof createdAt === "string" ? createdAt : undefined,
  };
}

/** `GET /spaces` — the named spaces the caller owns in the active org. */
export async function listSpaces(
  baseUrl: string,
  getToken: () => Promise<string | undefined>,
  orgId: string,
): Promise<Space[]> {
  const token = await getToken();
  const response = await fetch(`${baseUrl.replace(/\/$/, "")}/spaces`, {
    headers: contractHeaders(token, orgId),
  });
  if (!response.ok) {
    throw new Error(`GET /spaces failed (${response.status})`);
  }
  const payload = (await response.json()) as Record<string, unknown>;
  const list = Array.isArray(payload.spaces) ? payload.spaces : [];
  return list
    .map(normalizeSpace)
    .filter((s): s is Space => s !== null);
}

/**
 * `DELETE /spaces/{spaceId}` — permanently delete one of the caller's spaces in
 * the active org, purging its working knowledge graph (facts, snapshots, mounts).
 * Owner-scoped server-side: deleting a space you do not own 404s.
 */
export async function deleteSpace(
  baseUrl: string,
  getToken: () => Promise<string | undefined>,
  orgId: string,
  spaceId: string,
): Promise<void> {
  const token = await getToken();
  const response = await fetch(
    `${baseUrl.replace(/\/$/, "")}/spaces/${encodeURIComponent(spaceId)}`,
    {
      method: "DELETE",
      headers: contractHeaders(token, orgId),
    },
  );
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `DELETE /spaces/${spaceId} failed (${response.status})`);
  }
}

/** `POST /spaces` — create a named space owned by the caller in the active org. */
export async function createSpace(
  baseUrl: string,
  getToken: () => Promise<string | undefined>,
  orgId: string,
  body: { spaceId: string; name?: string },
): Promise<void> {
  const token = await getToken();
  const response = await fetch(`${baseUrl.replace(/\/$/, "")}/spaces`, {
    method: "POST",
    headers: contractHeaders(token, orgId),
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `POST /spaces failed (${response.status})`);
  }
}
