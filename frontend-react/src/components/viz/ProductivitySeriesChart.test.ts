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

  it("labels the two data scopes distinctly (GitHub commit activity vs Praxis ticket activity) and never asserts they describe one project's productivity (R25)", () => {
    const markup = renderToStaticMarkup(
      createElement(ProductivitySeriesChart, { series: SKEWED_SERIES }),
    );

    // The left (lines-of-code, S1-S3) axis/legend must name its scope as GitHub
    // commit activity, and the right (S4) axis/legend must name its scope as
    // Praxis ticket activity — distinctly, not just "Lines of code" / "Tickets
    // completed" with no source attribution.
    expect(markup).toMatch(/GitHub/i);
    expect(markup).toMatch(/Praxis/i);

    // No label may assert the combined view describes a single project's
    // productivity — the two series are drawn from different, unmapped sets
    // (the owner's GitHub repos vs. the org-wide Praxis ticket graph).
    expect(markup).not.toMatch(/single project's? productivity/i);
    expect(markup).not.toMatch(/same (set of )?projects?/i);
  });
});

// R27 acceptance: "given a 12-month range whose start precedes the instrumentation
// date, the S4 line is greyed before that date and carries a ticket-history-starts
// annotation" — a weekly-bucketed 12-month series whose window starts well before
// the instrumentation date, with several buckets on each side of the boundary (a
// single-point segment renders no path at all, so this must exercise a real curve).
const TWELVE_MONTH_BUCKETS = [
  "2025-08-01T00:00:00+00:00",
  "2025-11-01T00:00:00+00:00",
  "2026-07-01T00:00:00+00:00", // < instrumentation date: no real S4 data yet
  "2026-07-29T00:00:00+00:00", // >= instrumentation date: real recorded completions
  "2026-08-05T00:00:00+00:00",
];
function bucketedPoints(values: number[]) {
  return TWELVE_MONTH_BUCKETS.map((bucketStart, i) => ({ bucketStart, value: values[i] }));
}
const TWELVE_MONTH_SERIES: ProductivitySeries = {
  linesAdded: bucketedPoints([500, 700, 750, 800, 820]),
  linesDeleted: bucketedPoints([100, 150, 180, 200, 210]),
  netLines: bucketedPoints([400, 550, 570, 600, 610]),
  ticketsCompleted: bucketedPoints([0, 0, 0, 2, 3]),
};
const INSTRUMENTATION_DATE = "2026-07-25T07:17:15+00:00";

describe("ProductivitySeriesChart — S4 instrumentation-date greying (R27)", () => {
  it("greys S4 before the instrumentation date and shows a ticket-history-starts annotation when the range starts before it", () => {
    const markup = renderToStaticMarkup(
      createElement(ProductivitySeriesChart, {
        series: TWELVE_MONTH_SERIES,
        instrumentationDate: INSTRUMENTATION_DATE,
      }),
    );

    expect(markup).toContain('data-testid="s4-instrumentation-annotation"');
    expect(markup).toContain("Ticket history starts 2026-07-25");

    // The pre-instrumentation S4 segment is drawn in the grey/dashed style, the
    // post-instrumentation segment in the normal green style — two distinct S4
    // lines, not one.
    expect(markup).toContain("#9ca3af");
    expect(markup).toContain("#16a34a");
  });

  it("does not grey or annotate S4 when the whole window is after the instrumentation date", () => {
    const markup = renderToStaticMarkup(
      createElement(ProductivitySeriesChart, {
        series: TWELVE_MONTH_SERIES,
        instrumentationDate: "2025-01-01T00:00:00+00:00",
      }),
    );

    expect(markup).not.toContain('data-testid="s4-instrumentation-annotation"');
    expect(markup).not.toContain("#9ca3af");
  });

  it("does not grey or annotate S4 when no instrumentation date is known", () => {
    const markup = renderToStaticMarkup(
      createElement(ProductivitySeriesChart, { series: TWELVE_MONTH_SERIES }),
    );

    expect(markup).not.toContain('data-testid="s4-instrumentation-annotation"');
    expect(markup).not.toContain("#9ca3af");
  });
});
