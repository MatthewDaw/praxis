// @vitest-environment jsdom
//
// Acceptance (ticket 83f5e2cb63e54279ab328b944d57c61b): a frontend test renders the
// productivity panel across its loading, empty, partial-failure, first-run and
// key-missing states and asserts that each renders its own distinct treatment --
// given the frontend test runs, each of the five states renders a distinct element
// and the test fails if any two states become indistinguishable in the DOM.
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProductivityPanel } from "./ProductivityPanel";

const AUTH = { getToken: async () => "token-123", orgId: "org-1", spaceId: "space-1" };

function seriesBody(overrides: Record<string, unknown> = {}) {
  return JSON.stringify({
    range: "4weeks",
    truncated: false,
    repos_discovered: 1,
    spaces_count: 1,
    series: {
      s1_lines_added: [{ bucket_start: "2026-07-01", value: 0 }],
      s2_lines_deleted: [{ bucket_start: "2026-07-01", value: 0 }],
      s3_net_lines: [{ bucket_start: "2026-07-01", value: 0 }],
      s4_tickets_completed: [{ bucket_start: "2026-07-01", value: 0 }],
    },
    ...overrides,
  });
}

// One entry per acceptance state. `signature` is the testid that state -- and only that
// state -- must render; `respond` builds the mocked fetch response (or leaves the fetch
// pending forever, for "loading").
const STATES: Array<{
  name: string;
  signature: string;
  respond: () => Response | null;
}> = [
  { name: "loading", signature: "productivity-loading", respond: () => null },
  {
    name: "empty (all-zero activity, connected)",
    signature: "productivity-no-activity",
    respond: () => new Response(seriesBody(), { status: 200 }),
  },
  {
    name: "partial-failure (one series errored)",
    signature: "productivity-legend-error-linesAdded",
    respond: () =>
      new Response(
        seriesBody({
          errors: { s1_lines_added: { reason: "ticket series query failed" } },
          series: {
            s1_lines_added: [{ bucket_start: "2026-07-01", value: 100 }],
            s2_lines_deleted: [{ bucket_start: "2026-07-01", value: 20 }],
            s3_net_lines: [{ bucket_start: "2026-07-01", value: 80 }],
            s4_tickets_completed: [{ bucket_start: "2026-07-01", value: 2 }],
          },
        }),
        { status: 200 },
      ),
  },
  {
    name: "first-run (zero repos and zero spaces)",
    signature: "productivity-first-run",
    respond: () => new Response(seriesBody({ repos_discovered: 0, spaces_count: 0 }), { status: 200 }),
  },
  {
    name: "key-missing (GitHub key not configured)",
    signature: "productivity-key-status-missing",
    respond: () => new Response(JSON.stringify({ key_status: "missing" }), { status: 200 }),
  },
];

const ALL_SIGNATURES = STATES.map((s) => s.signature);

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ProductivityPanel across its five states (acceptance: distinct DOM per state)", () => {
  it.each(STATES)(
    "$name renders only its own signature, none of the other four states'",
    async ({ signature, respond }) => {
      const body = respond();
      const fetchMock = body
        ? vi.fn().mockResolvedValue(body)
        : vi.fn().mockImplementation(() => new Promise(() => {})); // "loading": never resolves
      vi.stubGlobal("fetch", fetchMock);

      render(<ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />);

      await waitFor(() => expect(screen.queryByTestId(signature)).not.toBeNull());

      // This is the state-under-test's own signature, and it's the ONLY one of the five
      // present -- if two states ever collapsed onto the same DOM treatment, one of these
      // "must be absent" checks would fail.
      for (const other of ALL_SIGNATURES) {
        if (other === signature) continue;
        expect(screen.queryByTestId(other)).toBeNull();
      }
    },
  );
});
