/**
 * The website half of R28's mailbox (the s-jobs surface).
 *
 * `postJobMessage` is a thin POST to the job's mailbox — the operator-facing sibling of an
 * equivalent MCP tool call, both ultimately invoking the same box-service mailbox action
 * (`knowledge.serve.box_service_mailbox.post_message`), mirroring how `resumeAction.ts` mirrors
 * `box_service_resume.resume_job`. It throws `PostMessageError` on any non-2xx response, and
 * refuses client-side (without a network call) on an empty message, so a caller (a compose-box
 * submit handler) can surface the failure rather than silently no-op.
 */

export class PostMessageError extends Error {
  readonly statusCode: number;

  constructor(message: string, statusCode: number) {
    super(message);
    this.name = "PostMessageError";
    this.statusCode = statusCode;
  }
}

export interface PostMessageAuth {
  getToken: () => Promise<string>;
  orgId: string;
}

/**
 * Post `text` to `jobId`'s mailbox. Refuses client-side when `text` is empty/whitespace-only,
 * mirroring the backend's own refusal (`box_service_mailbox.post_message`'s empty-message
 * `ValueError`) so the operator gets the same answer whichever surface asks.
 */
export async function postJobMessage(
  apiBaseUrl: string,
  jobId: string,
  text: string,
  auth: PostMessageAuth,
  fetchImpl: typeof fetch = fetch,
): Promise<void> {
  if (!text.trim()) {
    throw new PostMessageError("cannot post an empty mailbox message", 400);
  }

  const root = apiBaseUrl.replace(/\/$/, "");
  const token = await auth.getToken();
  const response = await fetchImpl(`${root}/jobs/${encodeURIComponent(jobId)}/messages`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      "X-Org-Id": auth.orgId,
    },
    body: JSON.stringify({ text }),
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new PostMessageError(
      `posting a message to job ${jobId} failed (${response.status}): ${detail || response.statusText}`,
      response.status,
    );
  }
}
