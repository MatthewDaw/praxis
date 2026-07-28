// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProductivityPanel } from "./ProductivityPanel";
import { formatShortDate } from "./viz/ProductivitySeriesChart";

const AUTH = { getToken: async () => "token-123", orgId: "org-1", spaceId: "space-1" };

// Every S1-S4 point is zero — the "no activity in this period" acceptance
// scenario: an all-zero chart conveys nothing but a flat floor, so no chart is
// drawn at all; the panel shows the no-activity caption instead, and must NOT be
// routed through the error-styling path. Distinct from the R20 first-run scenario
// (zero repos/spaces): this response has a real connected repo and space, they
// just did no work this window.
function allZeroResponseBody() {
  return JSON.stringify({
    range: "4weeks",
    truncated: false,
    repos_discovered: 1,
    spaces_count: 1,
    series: {
      s1_lines_added: [
        { bucket_start: "2026-07-01", value: 0 },
        { bucket_start: "2026-07-08", value: 0 },
      ],
      s2_lines_deleted: [
        { bucket_start: "2026-07-01", value: 0 },
        { bucket_start: "2026-07-08", value: 0 },
      ],
      s3_net_lines: [
        { bucket_start: "2026-07-01", value: 0 },
        { bucket_start: "2026-07-08", value: 0 },
      ],
      s4_tickets_completed: [
        { bucket_start: "2026-07-01", value: 0 },
        { bucket_start: "2026-07-08", value: 0 },
      ],
    },
  });
}

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
    const barGroups = container.querySelectorAll(".recharts-bar");
    expect(barGroups.length).toBe(4);
  });

  it("renders one aggregate chart plus one small chart per repo in series_by_repo, behind a collapsed-by-default section", async () => {
    const body = JSON.stringify({
      range: "4weeks",
      truncated: false,
      repos_discovered: 2,
      spaces_count: 1,
      series: {
        s1_lines_added: [{ bucket_start: "2026-07-01", value: 30 }],
        s2_lines_deleted: [{ bucket_start: "2026-07-01", value: 10 }],
        s3_net_lines: [{ bucket_start: "2026-07-01", value: 20 }],
        s4_tickets_completed: [{ bucket_start: "2026-07-01", value: 1 }],
      },
      series_by_repo: {
        "acme/one": {
          s1_lines_added: [{ bucket_start: "2026-07-01", value: 10 }],
          s2_lines_deleted: [{ bucket_start: "2026-07-01", value: 4 }],
          s3_net_lines: [{ bucket_start: "2026-07-01", value: 6 }],
        },
        "acme/two": {
          s1_lines_added: [{ bucket_start: "2026-07-01", value: 20 }],
          s2_lines_deleted: [{ bucket_start: "2026-07-01", value: 6 }],
          s3_net_lines: [{ bucket_start: "2026-07-01", value: 14 }],
        },
      },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 200 })));

    render(<ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />);

    await waitFor(() => {
      expect(screen.getByTestId("productivity-by-repo")).toBeInTheDocument();
    });

    // Collapsed on load: the header is there, the mini charts are not.
    const toggle = screen.getByTestId("productivity-by-repo-toggle");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("productivity-repo-chart-acme/one")).not.toBeInTheDocument();

    await userEvent.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("productivity-repo-chart-acme/one")).toBeInTheDocument();
    expect(screen.getByTestId("productivity-repo-chart-acme/two")).toBeInTheDocument();
    expect(screen.getByText("acme/one")).toBeInTheDocument();
    expect(screen.getByText("acme/two")).toBeInTheDocument();

    // ...and collapses again on a second click.
    await userEvent.click(toggle);
    expect(screen.queryByTestId("productivity-repo-chart-acme/one")).not.toBeInTheDocument();
  });

  it("renders one collapsible small chart per org in series_by_org, titled by org name", async () => {
    const body = JSON.stringify({
      range: "4weeks",
      truncated: false,
      repos_discovered: 1,
      spaces_count: 2,
      series: {
        s1_lines_added: [{ bucket_start: "2026-07-01", value: 30 }],
        s2_lines_deleted: [{ bucket_start: "2026-07-01", value: 10 }],
        s3_net_lines: [{ bucket_start: "2026-07-01", value: 20 }],
        s4_tickets_completed: [{ bucket_start: "2026-07-01", value: 5 }],
      },
      series_by_org: {
        "org-1": {
          name: "Acme Inc",
          s4_tickets_completed: [{ bucket_start: "2026-07-01", value: 3 }],
        },
        "org-2": {
          name: "Side Project",
          s4_tickets_completed: [{ bucket_start: "2026-07-01", value: 2 }],
        },
        // Every bucket zero: this org contributes no chart at all.
        "org-3": {
          name: "Dormant Org",
          s4_tickets_completed: [{ bucket_start: "2026-07-01", value: 0 }],
        },
      },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 200 })));

    render(<ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />);

    await waitFor(() => {
      expect(screen.getByTestId("productivity-by-org")).toBeInTheDocument();
    });

    const toggle = screen.getByTestId("productivity-by-org-toggle");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("productivity-org-chart-org-1")).not.toBeInTheDocument();

    await userEvent.click(toggle);

    const body_ = screen.getByTestId("productivity-by-org-body");
    expect(within(body_).getByText("Acme Inc")).toBeInTheDocument();
    expect(within(body_).getByText("Side Project")).toBeInTheDocument();
    expect(screen.queryByTestId("productivity-org-chart-org-3")).not.toBeInTheDocument();
  });

  it("renders no by-org section when series_by_org is absent from the response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(skewedResponseBody(), { status: 200 })),
    );

    render(<ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />);

    await waitFor(() => {
      expect(screen.queryByTestId("productivity-loading")).not.toBeInTheDocument();
    });
    expect(screen.queryByTestId("productivity-by-org")).not.toBeInTheDocument();
  });

  it("keeps a legend toggle in sync across the aggregate and every per-repo lines-of-code chart", async () => {
    const body = JSON.stringify({
      range: "4weeks",
      truncated: false,
      repos_discovered: 1,
      spaces_count: 1,
      series: {
        s1_lines_added: [{ bucket_start: "2026-07-01", value: 30 }],
        s2_lines_deleted: [{ bucket_start: "2026-07-01", value: 10 }],
        s3_net_lines: [{ bucket_start: "2026-07-01", value: 20 }],
        s4_tickets_completed: [{ bucket_start: "2026-07-01", value: 1 }],
      },
      series_by_repo: {
        "acme/one": {
          s1_lines_added: [{ bucket_start: "2026-07-01", value: 30 }],
          s2_lines_deleted: [{ bucket_start: "2026-07-01", value: 10 }],
          s3_net_lines: [{ bucket_start: "2026-07-01", value: 20 }],
        },
      },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 200 })));

    const { container } = render(
      <ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("productivity-by-repo-toggle")).toBeInTheDocument();
    });
    await userEvent.click(screen.getByTestId("productivity-by-repo-toggle"));

    // Two lines-of-code charts on screen (aggregate + one repo), three S1-S3 bars each.
    const linesAddedToggles = screen.getAllByTestId("productivity-legend-toggle-linesAdded");
    expect(linesAddedToggles.length).toBe(2);
    for (const toggle of linesAddedToggles) {
      expect(toggle).toHaveAttribute("aria-pressed", "true");
    }
    const barsBefore = container.querySelectorAll(".recharts-bar").length;

    // One click on the aggregate chart's legend...
    await userEvent.click(linesAddedToggles[0]);

    // ...flips BOTH charts' legend entries and drops one bar series from each.
    for (const toggle of screen.getAllByTestId("productivity-legend-toggle-linesAdded")) {
      expect(toggle).toHaveAttribute("aria-pressed", "false");
    }
    expect(container.querySelectorAll(".recharts-bar").length).toBe(barsBefore - 2);

    // Clicking a per-repo legend entry toggles it back on everywhere too.
    await userEvent.click(screen.getAllByTestId("productivity-legend-toggle-linesAdded")[1]);
    for (const toggle of screen.getAllByTestId("productivity-legend-toggle-linesAdded")) {
      expect(toggle).toHaveAttribute("aria-pressed", "true");
    }
    expect(container.querySelectorAll(".recharts-bar").length).toBe(barsBefore);
  });

  it("renders no by-repo section when series_by_repo is empty", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(skewedResponseBody(), { status: 200 })),
    );

    render(<ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />);

    await waitFor(() => {
      expect(screen.queryByTestId("productivity-loading")).not.toBeInTheDocument();
    });
    expect(screen.queryByTestId("productivity-by-repo")).not.toBeInTheDocument();
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
      repos_discovered: 1,
      spaces_count: 1,
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
      `Ticket history starts ${formatShortDate("2026-07-25T07:17:15+00:00")}`,
    );
  });

  it("shows a skeleton chart (no series path) while in flight, and removes it once data resolves", async () => {
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

    // In flight: a skeleton element is present and there is no chart series
    // path yet (no empty axes, no flash-of-zero chart).
    expect(screen.getByTestId("productivity-skeleton")).toBeInTheDocument();
    expect(container.querySelector(".recharts-rectangle")).toBeNull();
    expect(container.querySelector("svg.recharts-surface")).toBeNull();

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    resolveFetch(new Response(skewedResponseBody(), { status: 200 }));

    // Resolved: the skeleton is gone and the real chart (with series paths)
    // has taken its place.
    await waitFor(() => {
      expect(container.querySelector("svg.recharts-surface")).not.toBeNull();
    });
    expect(screen.queryByTestId("productivity-skeleton")).not.toBeInTheDocument();
    expect(container.querySelector(".recharts-rectangle")).not.toBeNull();
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

  it("drops every chart and shows just the no-activity caption, with no error styling, when every series is all zero", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(allZeroResponseBody(), { status: 200 })),
    );

    const { container } = render(
      <ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />,
    );

    // The no-activity caption stands in for both charts (scoped to this render's own
    // container — RTL does not auto-unmount between tests in this file, so a global
    // `screen` query could otherwise match a stale element left by an earlier test).
    await waitFor(() => {
      expect(container.querySelector('[data-testid="productivity-no-activity"]')).not.toBeNull();
    });

    // No empty plot area survives: a chart with nothing but zeros isn't drawn.
    expect(container.querySelector("svg.recharts-surface")).toBeNull();
    expect(container.querySelectorAll(".recharts-bar").length).toBe(0);

    // Never the error path/styling.
    expect(container.querySelector('[data-testid="productivity-error"]')).toBeNull();
    expect(container.querySelector(".productivity-panel__error")).toBeNull();
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
              s1_lines_added: [{ bucket_start: "2026-07-01", value: 40 }],
              s2_lines_deleted: [{ bucket_start: "2026-07-01", value: 15 }],
              s3_net_lines: [{ bucket_start: "2026-07-01", value: 25 }],
              s4_tickets_completed: [{ bucket_start: "2026-07-01", value: 2 }],
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
