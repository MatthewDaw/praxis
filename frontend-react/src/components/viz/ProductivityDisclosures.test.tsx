// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type { ProductivitySeries } from "../../api/contract";
import {
  DEFAULT_STATIC_CAVEATS,
  ProductivitySeriesChart,
  type ProductivityDisclosures,
} from "./ProductivitySeriesChart";

const SERIES: ProductivitySeries = {
  linesAdded: [{ bucketStart: "2026-07-01", value: 1200 }],
  linesDeleted: [{ bucketStart: "2026-07-01", value: 300 }],
  netLines: [{ bucketStart: "2026-07-01", value: 900 }],
  ticketsCompleted: [{ bucketStart: "2026-07-01", value: 1 }],
};

// Every disclosure condition D31 enumerates, on one load: the two static
// caveats (branch/fork exclusion, fixed timezone), four per-load conditions
// (rate-limited, N-unattributed, showing-first-N, stale-age), and the S4
// ticket-history start date.
const FULL_DISCLOSURES: ProductivityDisclosures = {
  staticCaveats: DEFAULT_STATIC_CAVEATS,
  perLoadConditions: [
    "Rate-limited — served from a 15-minute cache for this window.",
    "42 commits could not be attributed to an owner and are excluded.",
    "Showing only the first 500 of 1,240 matching points.",
    "Stale — this data was last refreshed 3 days ago.",
  ],
  ticketHistoryStart: "2026-06-01",
};

afterEach(() => {
  cleanup();
});

describe("ProductivitySeriesChart disclosures (R24)", () => {
  it("shows the static caveats in a persistent footnote strip beneath the chart", () => {
    render(<ProductivitySeriesChart series={SERIES} disclosures={FULL_DISCLOSURES} />);

    const strip = screen.getByTestId("productivity-footnote-strip");
    for (const caveat of FULL_DISCLOSURES.staticCaveats) {
      expect(strip).toHaveTextContent(caveat);
    }
  });

  it("keeps per-load conditions out of the plot area until the info affordance is opened, then reveals every one", async () => {
    render(<ProductivitySeriesChart series={SERIES} disclosures={FULL_DISCLOSURES} />);

    // Not dropped, but not cluttering the default view either.
    for (const condition of FULL_DISCLOSURES.perLoadConditions) {
      expect(screen.queryByText(condition)).not.toBeInTheDocument();
    }

    const affordance = screen.getByTestId("productivity-info-affordance");
    await userEvent.click(affordance);

    for (const condition of FULL_DISCLOSURES.perLoadConditions) {
      expect(screen.getByText(condition)).toBeInTheDocument();
    }
  });

  it("captions the ticket-history start date inline beside the S4 legend entry, not in the shared footnote", () => {
    render(<ProductivitySeriesChart series={SERIES} disclosures={FULL_DISCLOSURES} />);

    const caption = screen.getByTestId("productivity-s4-caption");
    expect(caption).toHaveTextContent("2026-06-01");
    expect(caption.closest("li")).toHaveTextContent("Tickets completed (S4)");

    const strip = screen.getByTestId("productivity-footnote-strip");
    expect(strip).not.toHaveTextContent("2026-06-01");
  });

  it("states the default-branch-only, fork-exclusion, squash-attribution and other-owner-exclusion caveats (R26)", () => {
    render(
      <ProductivitySeriesChart
        series={SERIES}
        disclosures={{ staticCaveats: DEFAULT_STATIC_CAVEATS, perLoadConditions: [] }}
      />,
    );

    const strip = screen.getByTestId("productivity-footnote-strip");
    expect(strip).toHaveTextContent(/default[- ]branch/i);
    expect(strip).toHaveTextContent(/fork/i);
    expect(strip).toHaveTextContent(/squash/i);
    expect(strip).toHaveTextContent(/merging author/i);
    expect(strip).toHaveTextContent(/private repositories/i);
    expect(strip).toHaveTextContent(/other individuals|owned by other/i);
  });

  it("never drops a disclosure: no condition, caveat, or the start date, is entirely absent from the DOM", async () => {
    render(<ProductivitySeriesChart series={SERIES} disclosures={FULL_DISCLOSURES} />);

    const affordance = screen.getByTestId("productivity-info-affordance");
    await userEvent.click(affordance);

    for (const caveat of FULL_DISCLOSURES.staticCaveats) {
      expect(screen.getByText(caveat)).toBeInTheDocument();
    }
    for (const condition of FULL_DISCLOSURES.perLoadConditions) {
      expect(screen.getByText(condition)).toBeInTheDocument();
    }
    expect(screen.getByTestId("productivity-s4-caption")).toHaveTextContent(
      FULL_DISCLOSURES.ticketHistoryStart as string,
    );
  });
});
