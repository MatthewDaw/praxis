import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ProductivitySeriesChart } from "./ProductivitySeriesChart";
import type { ProductivitySeries } from "../../api/contract";

// S1 (lines added) in the thousands, S4 (tickets completed) under five —
// exactly the acceptance scenario: a shared axis would flatten S4 onto the
// x-axis; independent axes must not.
const SKEWED_SERIES: ProductivitySeries = {
  linesAdded: [
    { bucketStart: "2026-07-01", value: 1200 },
    { bucketStart: "2026-07-08", value: 3400 },
    { bucketStart: "2026-07-15", value: 2100 },
    { bucketStart: "2026-07-22", value: 4800 },
  ],
  linesDeleted: [
    { bucketStart: "2026-07-01", value: 300 },
    { bucketStart: "2026-07-08", value: 900 },
    { bucketStart: "2026-07-15", value: 500 },
    { bucketStart: "2026-07-22", value: 1100 },
  ],
  netLines: [
    { bucketStart: "2026-07-01", value: 900 },
    { bucketStart: "2026-07-08", value: 2500 },
    { bucketStart: "2026-07-15", value: 1600 },
    { bucketStart: "2026-07-22", value: 3700 },
  ],
  ticketsCompleted: [
    { bucketStart: "2026-07-01", value: 1 },
    { bucketStart: "2026-07-08", value: 4 },
    { bucketStart: "2026-07-15", value: 2 },
    { bucketStart: "2026-07-22", value: 3 },
  ],
};

/** Extract every y-coordinate drawn along one recharts `<path>`'s `d` attribute. */
function pathYCoords(d: string): number[] {
  return [...d.matchAll(/-?\d+(?:\.\d+)?,(-?\d+(?:\.\d+)?)/g)].map((m) => Number(m[1]));
}

describe("ProductivitySeriesChart", () => {
  it("renders four lines (S1-S3 left axis, S4 right axis) against two independent y-axes", () => {
    const markup = renderToStaticMarkup(
      createElement(ProductivitySeriesChart, { series: SKEWED_SERIES }),
    );

    expect(markup).toContain("<svg");
    expect(markup).toContain("recharts-surface");

    const yAxisGroups = markup.match(/recharts-yAxis yAxis/g) ?? [];
    expect(yAxisGroups.length).toBe(2);
    expect(markup).toContain('orientation="right"');

    const lineGroups = markup.match(/class="recharts-layer recharts-line"/g) ?? [];
    expect(lineGroups.length).toBe(4);
  });

  it("does not flatten S4 onto the x-axis when S1 is in the thousands and S4 is under five", () => {
    const markup = renderToStaticMarkup(
      createElement(ProductivitySeriesChart, { series: SKEWED_SERIES, height: 320 }),
    );

    // Pull out each <path class="recharts-line-curve" ...> element's `d` attribute; recharts
    // renders one per <Line>, in declaration order (S1, S2, S3 left, S4 right).
    const paths = [...markup.matchAll(/recharts-line-curve"[^>]*\sd="([^"]+)"/g)].map(
      (m) => m[1],
    );
    expect(paths.length).toBe(4);

    const s4Ys = pathYCoords(paths[3]);
    expect(s4Ys.length).toBeGreaterThan(1);

    // "Flattened onto the x-axis" would mean every S4 point sits at (or within a
    // couple of px of) the bottom plot edge. With its own axis, S4's 1-4 range
    // should occupy real vertical spread, not collapse to the chart floor.
    const uniqueYs = new Set(s4Ys.map((y) => Math.round(y)));
    expect(uniqueYs.size).toBeGreaterThan(1);
    const spread = Math.max(...s4Ys) - Math.min(...s4Ys);
    expect(spread).toBeGreaterThan(10);
  });

  it("renders S1-S3 normally and marks the S4 legend entry with an error badge carrying its reason when the ticket series errored", () => {
    const markup = renderToStaticMarkup(
      createElement(ProductivitySeriesChart, {
        series: {
          ...SKEWED_SERIES,
          ticketsCompleted: [],
        },
        errors: { ticketsCompleted: "boom: ticket store unavailable" },
      }),
    );

    // S1-S3 still render as three normal lines (unaffected by the S4 failure).
    const lineGroups = markup.match(/class="recharts-layer recharts-line"/g) ?? [];
    expect(lineGroups.length).toBe(4);

    // The S4 legend entry carries an error badge naming the reason; S1-S3 do not.
    expect(markup).toContain('data-testid="productivity-legend-error-ticketsCompleted"');
    expect(markup).toContain("boom: ticket store unavailable");
    expect(markup).not.toContain('data-testid="productivity-legend-error-linesAdded"');
    expect(markup).not.toContain('data-testid="productivity-legend-error-linesDeleted"');
    expect(markup).not.toContain('data-testid="productivity-legend-error-netLines"');
  });
});
