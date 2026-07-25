// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProductivityPanel } from "./ProductivityPanel";

const AUTH = { getToken: async () => "token-123", orgId: "org-1", spaceId: "space-1" };

function bodyWithComputedAt(computedAt: string) {
  return JSON.stringify({
    range: "alltime",
    truncated: false,
    computed_at: computedAt,
    series: {
      s1_lines_added: [{ bucket_start: "2026-01-01", value: 10 }],
      s2_lines_deleted: [{ bucket_start: "2026-01-01", value: 2 }],
      s3_net_lines: [{ bucket_start: "2026-01-01", value: 8 }],
      s4_tickets_completed: [{ bucket_start: "2026-01-01", value: 1 }],
    },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ProductivityPanel Refresh control (acceptance)", () => {
  it(
    "given ten Refresh clicks within 1000ms on range=alltime, issues exactly one " +
      "outbound /productivity request and reflects the served computed_at",
    async () => {
      const responses = [
        bodyWithComputedAt("2026-07-25T00:00:00+00:00"),
        bodyWithComputedAt("2026-07-25T00:05:00+00:00"),
      ];
      const fetchMock = vi.fn().mockImplementation(() => {
        const idx = Math.min(fetchMock.mock.calls.length, responses.length) - 1;
        return Promise.resolve(new Response(responses[idx], { status: 200 }));
      });
      vi.stubGlobal("fetch", fetchMock);

      render(
        <ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} initialRange="alltime" />,
      );

      // Wait for the initial mount fetch to resolve and the last-updated label to
      // reflect the first response's computed_at.
      await waitFor(() => {
        expect(screen.getByTestId("productivity-last-updated")).toHaveTextContent(
          "2026-07-25T00:00:00+00:00",
        );
      });
      const callsBeforeRefresh = fetchMock.mock.calls.length;

      const refreshButton = screen.getByTestId("productivity-refresh");
      for (let i = 0; i < 10; i += 1) {
        fireEvent.click(refreshButton);
      }

      await waitFor(() => {
        expect(screen.getByTestId("productivity-last-updated")).toHaveTextContent(
          "2026-07-25T00:05:00+00:00",
        );
      });

      const callsFromClicks = fetchMock.mock.calls.length - callsBeforeRefresh;
      expect(callsFromClicks).toBe(1);
    },
  );
});
