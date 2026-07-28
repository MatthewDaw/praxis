// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type { ProductivitySeries } from "../../api/contract";
import {
  DEFAULT_STATIC_CAVEATS,
  formatShortDate,
  ProductivitySeriesChart,
  TicketsCompletedChart,
  type ProductivityDisclosures,
} from "./ProductivitySeriesChart";

const SERIES: ProductivitySeries = {
  linesAdded: [{ bucketStart: "2026-07-01", value: 1200 }],
  linesDeleted: [{ bucketStart: "2026-07-01", value: 300 }],
  netLines: [{ bucketStart: "2026-07-01", value: 900 }],
  ticketsCompleted: [{ bucketStart: "2026-07-01", value: 1 }],
};

const TICKET_HISTORY_START = "2026-06-01T00:00:00+00:00";

// Every disclosure condition D31 enumerates, on one load: the two static
// caveats (branch/fork exclusion, fixed timezone) and four per-load conditions
// (rate-limited, N-unattributed, showing-first-N, stale-age). The S4
// ticket-history start date is a separate chart's own prop (below), not part
// of this disclosures object.
const FULL_DISCLOSURES: ProductivityDisclosures = {
  staticCaveats: DEFAULT_STATIC_CAVEATS,
  perLoadConditions: [
    "Rate-limited — served from a 15-minute cache for this window.",
    "42 commits could not be attributed to an owner and are excluded.",
    "Showing only the first 500 of 1,240 matching points.",
    "Stale — this data was last refreshed 3 days ago.",
  ],
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

  it("captions the ticket-history start date inline beside the S4 legend entry, on the tickets chart itself", () => {
    render(
      <TicketsCompletedChart series={SERIES} instrumentationDate={TICKET_HISTORY_START} />,
    );

    const caption = screen.getByTestId("productivity-s4-caption");
    expect(caption).toHaveTextContent(formatShortDate(TICKET_HISTORY_START));
    expect(caption.closest("li")).toHaveTextContent("Tickets completed (S4)");
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

  it("never drops a disclosure: no condition or caveat is entirely absent from the DOM", async () => {
    render(<ProductivitySeriesChart series={SERIES} disclosures={FULL_DISCLOSURES} />);

    const affordance = screen.getByTestId("productivity-info-affordance");
    await userEvent.click(affordance);

    for (const caveat of FULL_DISCLOSURES.staticCaveats) {
      expect(screen.getByText(caveat)).toBeInTheDocument();
    }
    for (const condition of FULL_DISCLOSURES.perLoadConditions) {
      expect(screen.getByText(condition)).toBeInTheDocument();
    }
  });

  it("never drops the S4 ticket-history start date from the tickets chart's own legend", () => {
    render(
      <TicketsCompletedChart series={SERIES} instrumentationDate={TICKET_HISTORY_START} />,
    );
    expect(screen.getByTestId("productivity-s4-caption")).toBeInTheDocument();
  });
});
