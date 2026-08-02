// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { candidateFromMapping } from "../api/candidateModel";
import { AuditTimeline } from "./AuditTimeline";

/**
 * The audit trail is how a human notices a provenance regression by GLANCING at a ticket
 * rather than by byte-diffing a pre-move dump. These pin the two properties that make
 * that possible: the count is always visible (a collapsed trail can't read as a short
 * one), and every entry shape in the real data renders — including the `compacted`
 * marker's note, which is the only record that history was deliberately dropped.
 *
 * Shapes below are taken from appeal_engine's prd snapshot.
 */
const STORED_TRAIL = [
  {
    actor: "human-gate",
    action: "created",
    timestamp: "2026-01-01T00:00:00Z",
    provenance: "prd-appeal_engine",
  },
  {
    actor: "af-build/appeal_engine",
    action: "edited",
    timestamp: "2026-01-02T00:00:00Z",
    provenance: "prd-appeal_engine",
  },
  {
    actor: "af-intake-plan-perf-compaction",
    action: "compacted",
    timestamp: "2026-01-03T00:00:00Z",
    provenance: "prd-appeal_engine",
    note: "compacted 32 entries to first+last; removed entries were per-heartbeat af-build edit records with no distinct information",
  },
  {
    actor: "human-gate",
    action: "edited",
    timestamp: "2026-01-04T00:00:00Z",
    provenance: "prd-appeal_engine",
  },
  // Heterogeneous on purpose: no provenance, and a key the renderer has never seen.
  { actor: "praxis", action: "moved", timestamp: "2026-01-05T00:00:00Z", fromSnapshot: "prd-old" },
];

function trailFromStoredMeta() {
  return candidateFromMapping({
    id: "f1",
    title: "TMP-1",
    content: "body",
    state: "active",
    meta: { auditTrail: STORED_TRAIL },
    auditTrail: STORED_TRAIL,
  } as never).auditTrail;
}

afterEach(cleanup);

describe("AuditTimeline", () => {
  it("states the entry count and how many are shown while collapsed", () => {
    render(<AuditTimeline entries={trailFromStoredMeta()} />);
    expect(screen.getByText(/5 entries · showing 3/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Show all 5 entries \(2 hidden\)/ }),
    ).toBeInTheDocument();
  });

  it("renders every stored entry, in order, once expanded", async () => {
    const entries = trailFromStoredMeta();
    render(<AuditTimeline entries={entries} />);
    await userEvent.click(screen.getByRole("button", { name: /Show all 5 entries/ }));

    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(STORED_TRAIL.length);
    // Chronological order is the point of a trail — assert the rendered sequence, not
    // just the set.
    expect(items.map((li) => li.querySelector(".audit-timeline__action")?.textContent)).toEqual(
      STORED_TRAIL.map((e) => e.action),
    );
  });

  it("shows a compaction note — the only record that history was deliberately dropped", async () => {
    render(<AuditTimeline entries={trailFromStoredMeta()} />);
    await userEvent.click(screen.getByRole("button", { name: /Show all/ }));
    expect(screen.getByText(/compacted 32 entries to first\+last/)).toBeInTheDocument();
  });

  it("renders an entry missing a known key, and keeps unrecognized keys visible", async () => {
    render(<AuditTimeline entries={trailFromStoredMeta()} />);
    await userEvent.click(screen.getByRole("button", { name: /Show all/ }));
    expect(screen.getByText("moved")).toBeInTheDocument();
    expect(screen.getByText("no provenance")).toBeInTheDocument();
    expect(screen.getByText(/fromSnapshot/)).toBeInTheDocument();
  });

  it("renders the whole trail with no toggle when it is already short", () => {
    render(<AuditTimeline entries={trailFromStoredMeta().slice(0, 2)} />);
    expect(screen.getByText(/^2 entries$/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Show all/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("carries the stored trail through the model entry for entry", () => {
    const parsed = trailFromStoredMeta();
    expect(parsed).toHaveLength(STORED_TRAIL.length);
    parsed.forEach((entry, i) => {
      // `as unknown as` because the union's optional members (`note?: undefined`) do not overlap
      // an index signature of `string`, so a direct assertion is a TS2352 error.
      const stored = STORED_TRAIL[i] as unknown as Record<string, string | undefined>;
      expect(entry.action).toBe(stored.action);
      expect(entry.actor).toBe(stored.actor);
      expect(entry.timestamp).toBe(stored.timestamp);
      expect(entry.note).toBe(stored.note);
    });
  });
});
