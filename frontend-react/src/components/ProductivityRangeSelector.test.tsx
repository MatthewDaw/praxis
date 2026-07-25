// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProductivityPanel } from "./ProductivityPanel";

const AUTH = { getToken: async () => "token-123", orgId: "org-1", spaceId: "space-1" };

function responseBody(range: string, bucketUnit: string) {
  return JSON.stringify({
    range,
    truncated: false,
    repos_discovered: 1,
    spaces_count: 1,
    bucket_unit: bucketUnit,
    series: {
      s1_lines_added: [{ bucket_start: "2026-07-01", value: 100 }],
      s2_lines_deleted: [{ bucket_start: "2026-07-01", value: 20 }],
      s3_net_lines: [{ bucket_start: "2026-07-01", value: 80 }],
      s4_tickets_completed: [{ bucket_start: "2026-07-01", value: 2 }],
    },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ProductivityPanel range dropdown (R16)", () => {
  it("opens defaulted to 'Last 4 weeks', and selecting 'Last 12 months' re-queries range=12months and relabels the axis to weekly buckets", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const range = new URL(url).searchParams.get("range") ?? "";
      const bucketUnit = range === "12months" ? "week" : "day";
      return Promise.resolve(new Response(responseBody(range, bucketUnit), { status: 200 }));
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container } = render(
      <ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />,
    );

    // Fresh page: the dropdown defaults to "Last 4 weeks" and the initial
    // fetch requests range=4weeks.
    const select = (await screen.findByTestId("productivity-range")) as HTMLSelectElement;
    expect(select.value).toBe("4weeks");
    expect(screen.getByText("Last 4 weeks")).toBeInTheDocument();
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("range=4weeks"), expect.anything());
    });
    await waitFor(() => {
      expect(container.querySelector("svg.recharts-surface")).not.toBeNull();
    });

    // Selecting "Last 12 months" issues a request with range=12months and
    // relabels the x-axis to weekly buckets.
    fireEvent.change(select, { target: { value: "12months" } });

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("range=12months"),
        expect.anything(),
      );
    });
    await waitFor(() => {
      expect(container.textContent).toContain("Weekly buckets");
    });
  });
});
