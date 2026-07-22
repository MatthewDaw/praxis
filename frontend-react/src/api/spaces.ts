import { contractHeaders } from "./contract";

/**
 * A space is a named container for the org-shared snapshots a login saves/loads
 * within an org. Selecting one sends `X-Praxis-Space`, which (paired with
 * `X-Praxis-Snapshot`) selects the snapshot folder to save into/load from. It
 * does NOT scope working memory: the backend keys the live facts graph on
 * `(org, sub)` alone — there is no `user_id` mangling.
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

async function spaceRequest(
  baseUrl: string,
  getToken: () => Promise<string | undefined>,
  orgId: string,
  method: string,
  path: string,
  body?: unknown,
): Promise<Response> {
  const token = await getToken();
  const response = await fetch(`${baseUrl.replace(/\/$/, "")}${path}`, {
    method,
    headers: contractHeaders(token, orgId),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `${method} ${path} failed (${response.status})`);
  }
  return response;
}

/** `GET /spaces` — the named spaces the caller owns in the active org. */
export async function listSpaces(
  baseUrl: string,
  getToken: () => Promise<string | undefined>,
  orgId: string,
): Promise<Space[]> {
  const response = await spaceRequest(baseUrl, getToken, orgId, "GET", "/spaces");
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
  await spaceRequest(
    baseUrl,
    getToken,
    orgId,
    "DELETE",
    `/spaces/${encodeURIComponent(spaceId)}`,
  );
}

/**
 * `PATCH /spaces/{spaceId}` — rename one of the caller's spaces (display name
 * only; `spaceId` is the immutable key). Renaming a space you do not own 404s.
 */
export async function renameSpace(
  baseUrl: string,
  getToken: () => Promise<string | undefined>,
  orgId: string,
  spaceId: string,
  name: string,
): Promise<void> {
  await spaceRequest(
    baseUrl,
    getToken,
    orgId,
    "PATCH",
    `/spaces/${encodeURIComponent(spaceId)}`,
    { name },
  );
}

/** `POST /spaces` — create a named space owned by the caller in the active org. */
export async function createSpace(
  baseUrl: string,
  getToken: () => Promise<string | undefined>,
  orgId: string,
  body: { spaceId: string; name?: string },
): Promise<void> {
  await spaceRequest(baseUrl, getToken, orgId, "POST", "/spaces", body);
}
