import { describe, expect, it } from "vitest";

import { deliveryStatus, jobMailboxView, mailboxMessageView, type MailboxMessage } from "./mailboxView";

describe("deliveryStatus", () => {
  it("is undelivered when surfacedAt is null", () => {
    expect(deliveryStatus(null)).toBe("undelivered");
  });

  it("is delivered once surfacedAt is set", () => {
    expect(deliveryStatus("2026-07-27T00:00:00Z")).toBe("delivered");
  });
});

describe("mailboxMessageView", () => {
  it("shows a message that never reached a ticket boundary as undelivered with its posted timestamp and no surfaced timestamp", () => {
    const message: MailboxMessage = {
      text: "are you stuck?",
      postedAt: "2026-07-27T01:00:00Z",
      surfacedAt: null,
    };

    const view = mailboxMessageView(message);

    expect(view.status).toBe("undelivered");
    expect(view.postedAt).toBe("2026-07-27T01:00:00Z");
    expect(view.surfacedAt).toBeNull();
  });

  it("shows a surfaced message as delivered with both timestamps", () => {
    const message: MailboxMessage = {
      text: "status?",
      postedAt: "2026-07-27T01:00:00Z",
      surfacedAt: "2026-07-27T01:05:00Z",
    };

    const view = mailboxMessageView(message);

    expect(view.status).toBe("delivered");
    expect(view.surfacedAt).toBe("2026-07-27T01:05:00Z");
  });
});

describe("jobMailboxView", () => {
  it("maps every message to its delivery view in order", () => {
    const messages: MailboxMessage[] = [
      { text: "first", postedAt: "t1", surfacedAt: "t1s" },
      { text: "second", postedAt: "t2", surfacedAt: null },
    ];

    const views = jobMailboxView(messages);

    expect(views.map((v) => v.status)).toEqual(["delivered", "undelivered"]);
  });
});
