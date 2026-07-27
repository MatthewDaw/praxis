import { describe, expect, it, vi } from "vitest";

import { PostMessageError, postJobMessage } from "./postMessageAction";

const AUTH = { getToken: async () => "token-123", orgId: "org-1" };

describe("postJobMessage", () => {
  it("POSTs the message body to the job's mailbox endpoint with the operator's credentials", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));

    await postJobMessage("http://127.0.0.1:8000", "job-42", "please pause", AUTH, fetchImpl);

    expect(fetchImpl).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/jobs/job-42/messages",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          Authorization: "Bearer token-123",
          "X-Org-Id": "org-1",
        }),
        body: JSON.stringify({ text: "please pause" }),
      }),
    );
  });

  it("refuses client-side for an empty message without a network call", async () => {
    const fetchImpl = vi.fn();

    await expect(
      postJobMessage("http://127.0.0.1:8000", "job-42", "   ", AUTH, fetchImpl),
    ).rejects.toThrow(PostMessageError);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("surfaces a non-2xx response as a PostMessageError", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response("job not found", { status: 404 }));

    await expect(
      postJobMessage("http://127.0.0.1:8000", "job-42", "hello", AUTH, fetchImpl),
    ).rejects.toThrow(/404/);
  });
});
