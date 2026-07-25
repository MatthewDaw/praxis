// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProductivityPanel } from "./ProductivityPanel";

const AUTH = { getToken: async () => "token-123", orgId: "org-1", spaceId: "space-1" };

// S1 (lines added) in the thousands, S4 (tickets completed) under five —
// the acceptance scenario the panel's chart must handle without flattening
// S4 onto the x-axis.
function skewedResponseBody() {
  return JSON.stringify({
    range: "4weeks",
    truncated: false,
    repos_discovered: 1,
    spaces_count: 1,
    series: {
      s1_lines_added: [
        { bucket_start: "2026-07-01", value: 1200 },
        { bucket_start: "2026-07-08", value: 3400 },
      ],
      s2_lines_deleted: [
        { bucket_start: "2026-07-01", value: 300 },
        { bucket_start: "2026-07-08", value: 900 },
      ],
      s3_net_lines: [
        { bucket_start: "2026-07-01", value: 900 },
        { bucket_start: "2026-07-08", value: 2500 },
      ],
      s4_tickets_completed: [
        { bucket_start: "2026-07-01", value: 1 },
        { bucket_start: "2026-07-08", value: 4 },
      ],
    },
  });
}

afterEach(() => {
  // This file's vitest config runs with no global `afterEach`, so
  // `@testing-library/react`'s auto-cleanup never registers itself -- without an
  // explicit `cleanup()` here, a previous test's rendered DOM (e.g. its own
  // `data-testid="productivity-error"`) would still be present when the next
  // test queries the document, producing false "found stale element" failures.
  cleanup();
  vi.unstubAllGlobals();
  cleanup();
});

describe("ProductivityPanel", () => {
  it("fetches /productivity and renders a multi-series dual-axis chart with S1-S3 on the left axis and S4 on the right", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(skewedResponseBody(), { status: 200 })),
    );

    const { container } = render(
      <ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />,
    );

    await waitFor(() => {
      expect(container.querySelector("svg.recharts-surface")).not.toBeNull();
    });

    const yAxisGroups = container.querySelectorAll(".recharts-yAxis");
    expect(yAxisGroups.length).toBe(2);
    const lineGroups = container.querySelectorAll(".recharts-line");
    expect(lineGroups.length).toBe(4);
  });

  it("shows a loading state before the fetch resolves and no chart yet", async () => {
    let resolveFetch: (value: Response) => void = () => {};
    const fetchMock = vi.fn().mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { container } = render(
      <ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />,
    );

    expect(screen.getByTestId("productivity-loading")).toBeInTheDocument();
    expect(container.querySelector("svg.recharts-surface")).toBeNull();

    // Wait until the panel's effect has actually invoked `fetch` (it does so only
    // after an async `resolveToken` tick) before resolving it, or this resolves a
    // stale pre-call promise and the real fetch call hangs forever.
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    resolveFetch(new Response(skewedResponseBody(), { status: 200 }));
    await waitFor(() => {
      expect(container.querySelector("svg.recharts-surface")).not.toBeNull();
    });
  });

  it("greys S4 and shows the ticket-history-starts annotation when the response carries an instrumentation date preceding the window (R27)", async () => {
    const body = JSON.stringify({
      range: "12months",
      truncated: false,
      s4_instrumentation_date: "2026-07-25T07:17:15+00:00",
      series: {
        s1_lines_added: [
          { bucket_start: "2025-08-01T00:00:00+00:00", value: 500 },
          { bucket_start: "2026-07-01T00:00:00+00:00", value: 800 },
        ],
        s2_lines_deleted: [
          { bucket_start: "2025-08-01T00:00:00+00:00", value: 100 },
          { bucket_start: "2026-07-01T00:00:00+00:00", value: 200 },
        ],
        s3_net_lines: [
          { bucket_start: "2025-08-01T00:00:00+00:00", value: 400 },
          { bucket_start: "2026-07-01T00:00:00+00:00", value: 600 },
        ],
        s4_tickets_completed: [
          { bucket_start: "2025-08-01T00:00:00+00:00", value: 0 },
          { bucket_start: "2026-07-01T00:00:00+00:00", value: 3 },
        ],
      },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 200 })));

    render(<ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />);

    await waitFor(() => {
      expect(screen.getByTestId("s4-instrumentation-annotation")).toBeInTheDocument();
    });
    expect(screen.getByTestId("s4-instrumentation-annotation")).toHaveTextContent(
      "Ticket history starts 2026-07-25",
    );
  });

  it("shows an error state when the fetch fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("forbidden", { status: 403 })),
    );

    render(<ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />);

    await waitFor(() => {
      expect(screen.getByTestId("productivity-error")).toBeInTheDocument();
    });
  });

  // R22 acceptance: given a response carrying a stale flag and a computed_at, the
  // panel displays the computed_at age and a stale marker adjacent to the chart.
  it("renders the computed_at age and a stale marker adjacent to the chart when the response is stale", async () => {
    const fiveMinutesAgo = new Date(Date.now() - 5 * 60 * 1000).toISOString();
    const body = JSON.stringify({
      ...JSON.parse(skewedResponseBody()),
      stale: true,
      computed_at: fiveMinutesAgo,
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 200 })));

    const { container } = render(
      <ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />,
    );

    await waitFor(() => {
      expect(container.querySelector("svg.recharts-surface")).not.toBeNull();
    });

    const marker = screen.getByTestId("productivity-stale-marker");
    expect(marker).toBeInTheDocument();
    expect(marker.textContent).toContain("Stale");
    expect(screen.getByTestId("productivity-computed-age").textContent).toBe("5m old");
    // Adjacent to the chart: the marker and the chart share the same panel section.
    expect(container.querySelector(".productivity-panel")?.contains(marker)).toBe(true);
    expect(
      container.querySelector(".productivity-panel")?.querySelector("svg.recharts-surface"),
    ).not.toBeNull();
  });

  it("labels the marker Rate-limited when the stale response was caused by a rate limit", async () => {
    const body = JSON.stringify({
      ...JSON.parse(skewedResponseBody()),
      stale: true,
      rate_limited: true,
      computed_at: new Date(Date.now() - 30 * 1000).toISOString(),
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 200 })));

    render(<ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />);

    await waitFor(() => {
      expect(screen.getByTestId("productivity-stale-marker").textContent).toContain(
        "Rate-limited",
      );
    });
  });

  it("renders no stale marker for a fresh (non-stale) response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(skewedResponseBody(), { status: 200 })),
    );

    const { container } = render(
      <ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />,
    );

    await waitFor(() => {
      expect(container.querySelector("svg.recharts-surface")).not.toBeNull();
    });
    expect(screen.queryByTestId("productivity-stale-marker")).toBeNull();
  });

  it("shows a first-run message and no chart when zero repos and zero spaces are reported", async () => {
    const firstRunBody = JSON.stringify({
      range: "4weeks",
      truncated: false,
      repos_discovered: 0,
      spaces_count: 0,
      series: {
        s1_lines_added: [{ bucket_start: "2026-07-01", value: 0 }],
        s2_lines_deleted: [{ bucket_start: "2026-07-01", value: 0 }],
        s3_net_lines: [{ bucket_start: "2026-07-01", value: 0 }],
        s4_tickets_completed: [{ bucket_start: "2026-07-01", value: 0 }],
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(firstRunBody, { status: 200 })),
    );

    const { container } = render(
      <ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />,
    );

    await waitFor(() => {
      expect(container.querySelector('[data-testid="productivity-first-run"]')).not.toBeNull();
    });
    expect(container.querySelector("svg.recharts-surface")).toBeNull();
  });

  it("renders the chart (not the first-run message) when repos or spaces are non-zero", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            range: "4weeks",
            truncated: false,
            repos_discovered: 1,
            spaces_count: 0,
            series: {
              s1_lines_added: [{ bucket_start: "2026-07-01", value: 0 }],
              s2_lines_deleted: [{ bucket_start: "2026-07-01", value: 0 }],
              s3_net_lines: [{ bucket_start: "2026-07-01", value: 0 }],
              s4_tickets_completed: [{ bucket_start: "2026-07-01", value: 0 }],
            },
          }),
          { status: 200 },
        ),
      ),
    );

    const { container } = render(
      <ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />,
    );

    await waitFor(() => {
      expect(container.querySelector("svg.recharts-surface")).not.toBeNull();
    });
    expect(container.querySelector('[data-testid="productivity-first-run"]')).toBeNull();
  });

  // R21 acceptance: given the backend reports a key status of missing, expired or
  // insufficient_scope, the panel renders the matching operator message and never a
  // raw 401/403 or a user-facing "connect your GitHub" prompt.
  describe("GitHub key status (R21)", () => {
    const cases: Array<{
      keyStatus: "missing" | "expired" | "insufficient_scope";
      testId: string;
      expectedSnippet: string;
    }> = [
      { keyStatus: "missing", testId: "productivity-key-status-missing", expectedSnippet: "not configured" },
      { keyStatus: "expired", testId: "productivity-key-status-expired", expectedSnippet: "rejected" },
      {
        keyStatus: "insufficient_scope",
        testId: "productivity-key-status-insufficient-scope",
        expectedSnippet: "Contents: Read",
      },
    ];

    // Every distinct message text seen across cases, asserted unique at the end -- a
    // single shared `it.each` run rather than a second full render loop.
    const seenMessages = new Set<string>();

    it.each(cases)(
      "renders the $keyStatus operator message and no connect button, never the chart",
      async ({ keyStatus, testId, expectedSnippet }) => {
        vi.stubGlobal(
          "fetch",
          vi.fn().mockResolvedValue(
            new Response(JSON.stringify({ key_status: keyStatus }), { status: 200 }),
          ),
        );

        const { container, getByTestId, queryByTestId } = render(
          <ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />,
        );

        await waitFor(() => {
          expect(getByTestId(testId)).toBeInTheDocument();
        });
        const messageText = getByTestId(testId).textContent ?? "";
        expect(messageText).toContain(expectedSnippet);
        seenMessages.add(messageText);

        // Never a raw 401/403 string surfaced as the (unrelated) error state...
        expect(queryByTestId("productivity-error")).not.toBeInTheDocument();
        // ...never the chart...
        expect(container.querySelector("svg.recharts-surface")).toBeNull();
        // ...and never a connect-your-GitHub button/link.
        expect(container.querySelector("button, a[role='button']")).toBeNull();
        expect(container.textContent).not.toMatch(/connect.*github/i);
      },
    );

    it("renders a distinct message per key status (never a generic one)", () => {
      expect(seenMessages.size).toBe(cases.length);
    });
  });
});
