// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  // This file's vitest config runs with no global `afterEach`, so
  // `@testing-library/react`'s auto-cleanup never registers itself -- without an
  // explicit `cleanup()` here, a previous test's rendered DOM stays mounted and
  // `findByTestId` fails with "found multiple elements" on the next test.
  cleanup();
  vi.unstubAllGlobals();
});

describe("ProductivityPanel bin-by dropdown", () => {
  it("defaults to Day and issues the initial fetch with bucketUnit=day", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const bucketUnit = new URL(url).searchParams.get("bucketUnit") ?? "";
      return Promise.resolve(new Response(responseBody("4weeks", bucketUnit), { status: 200 }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />);

    const select = (await screen.findByTestId("productivity-bucket-unit")) as HTMLSelectElement;
    expect(select.value).toBe("day");
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("bucketUnit=day"),
        expect.anything(),
      );
    });
  });

  it("selecting Week refetches with bucketUnit=week", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const bucketUnit = new URL(url).searchParams.get("bucketUnit") ?? "";
      return Promise.resolve(new Response(responseBody("4weeks", bucketUnit), { status: 200 }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />);

    const select = (await screen.findByTestId("productivity-bucket-unit")) as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe("day"));

    fireEvent.change(select, { target: { value: "week" } });

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("bucketUnit=week"),
        expect.anything(),
      );
    });
    expect(select.value).toBe("week");
  });

  it("selecting Month then switching range keeps Month selected (independent controls)", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const parsed = new URL(url);
      const range = parsed.searchParams.get("range") ?? "";
      const bucketUnit = parsed.searchParams.get("bucketUnit") ?? "";
      return Promise.resolve(new Response(responseBody(range, bucketUnit), { status: 200 }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ProductivityPanel apiBaseUrl="http://127.0.0.1:8000" auth={AUTH} />);

    const initialBucketSelect = (await screen.findByTestId(
      "productivity-bucket-unit",
    )) as HTMLSelectElement;

    fireEvent.change(initialBucketSelect, { target: { value: "month" } });
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("bucketUnit=month"),
        expect.anything(),
      );
    });

    // The controls unmount while `loading` is true and remount once the fetch settles
    // (see ProductivityPanel), so re-query both selects fresh rather than reuse stale
    // node references from before the bucket-unit change.
    const rangeSelect = (await screen.findByTestId("productivity-range")) as HTMLSelectElement;
    fireEvent.change(rangeSelect, { target: { value: "12months" } });
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("range=12months"),
        expect.anything(),
      );
    });

    // The bin-by selection was NOT reset by the range change.
    const bucketSelect = (await screen.findByTestId(
      "productivity-bucket-unit",
    )) as HTMLSelectElement;
    expect(bucketSelect.value).toBe("month");
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/range=12months.*bucketUnit=month|bucketUnit=month.*range=12months/),
        expect.anything(),
      );
    });
  });
});
