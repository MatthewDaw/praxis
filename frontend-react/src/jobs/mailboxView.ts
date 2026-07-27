/**
 * The website half of R72's mailbox job view (the s-jobs surface).
 *
 * The mailbox (`box_service_mailbox.py`) records both a `postedAt` and a `surfacedAt` timestamp
 * per message; `surfacedAt` is set only when the injected Stop hook actually drains the message at
 * a ticket boundary. A message posted to a session that never reaches a ticket boundary keeps
 * `surfacedAt === null` forever, so `mailboxMessageView` derives `"undelivered"` from that alone —
 * never from how long ago the message was posted or whether the session still looks alive — mirroring
 * the backend's `delivery_status` so the two can never disagree on what "delivered" means.
 */

export type DeliveryStatus = "delivered" | "undelivered";

export interface MailboxMessage {
  text: string;
  postedAt: string;
  surfacedAt: string | null;
}

export interface MailboxMessageView extends MailboxMessage {
  status: DeliveryStatus;
}

/** A message is `"delivered"` iff it has been surfaced at a ticket boundary — otherwise it is
 * `"undelivered"`, regardless of elapsed time since posting. */
export function deliveryStatus(surfacedAt: string | null): DeliveryStatus {
  return surfacedAt === null ? "undelivered" : "delivered";
}

/** Attach the derived delivery status to one mailbox message, unchanged otherwise — the job view
 * renders `postedAt` always and `surfacedAt` only when the message is delivered. */
export function mailboxMessageView(message: MailboxMessage): MailboxMessageView {
  return { ...message, status: deliveryStatus(message.surfacedAt) };
}

/** The job view's mailbox section: every posted message with its derived delivery status, in the
 * order the mailbox returned them. */
export function jobMailboxView(messages: readonly MailboxMessage[]): MailboxMessageView[] {
  return messages.map(mailboxMessageView);
}
