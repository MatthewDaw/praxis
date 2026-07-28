import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  formatBucketTick,
  formatShortDate,
  ProductivitySeriesChart,
  TicketsCompletedChart,
} from "./ProductivitySeriesChart";
import type { ProductivitySeries } from "../../api/contract";

// S1 (lines added) in the thousands, S4 (tickets completed) under five — the two series
// are charted separately now (R25: unrelated data sources on unrelated scales), so there
// is no shared-axis flattening risk to assert against, but each chart's own data must
// still render with real vertical spread.
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

/** Extract the `y` and `height` attributes off every rendered recharts Bar rectangle
 * (recharts draws each Bar segment as a `<path class="recharts-rectangle" ...>` carrying
 * literal `x`/`y`/`width`/`height` attributes, not a `<rect>` element). */
function barRectDims(markup: string): { y: number; height: number }[] {
  return [...markup.matchAll(/<path[^>]*class="recharts-rectangle"[^>]*>/g)].map((m) => {
    const tag = m[0];
    const y = Number(tag.match(/\sy="(-?\d+(?:\.\d+)?)"/)?.[1]);
    const height = Number(tag.match(/\sheight="(-?\d+(?:\.\d+)?)"/)?.[1]);
    return { y, height };
  });
}

describe("formatBucketTick", () => {
  it("never renders a raw ISO-8601 string", () => {
    const label = formatBucketTick("2026-07-27T02:25:53.424538+00:00", "day");
    expect(label).not.toMatch(/^\d{4}-\d{2}-\d{2}/);
    expect(label).not.toContain("T");
  });

  it("falls back to the raw value when it doesn't parse as a date", () => {
    expect(formatBucketTick("not-a-date")).toBe("not-a-date");
  });
});

describe("formatShortDate", () => {
  it("never renders a raw ISO-8601 string", () => {
    const label = formatShortDate("2026-07-25T07:17:15+00:00");
    expect(label).not.toMatch(/^\d{4}-\d{2}-\d{2}/);
  });
});

describe("ProductivitySeriesChart (S1-S3, lines of code)", () => {
  it("renders three lines against a single y-axis, one svg chart", () => {
    const markup = renderToStaticMarkup(
      createElement(ProductivitySeriesChart, { series: SKEWED_SERIES }),
    );

    expect(markup).toContain("<svg");
    expect(markup).toContain("recharts-surface");

    const yAxisGroups = markup.match(/recharts-yAxis yAxis/g) ?? [];
    expect(yAxisGroups.length).toBe(1);
    expect(markup).not.toContain('orientation="right"');

    const barGroups = markup.match(/class="recharts-layer recharts-bar"/g) ?? [];
    expect(barGroups.length).toBe(3);
  });

  it("never renders a raw ISO-8601 tick label", () => {
    const markup = renderToStaticMarkup(
      createElement(ProductivitySeriesChart, { series: SKEWED_SERIES, bucketUnit: "day" }),
    );
    expect(markup).not.toMatch(/2026-07-\d{2}T/);
  });

  it("labels the chart's scope as GitHub commit activity and never asserts it is one project's productivity (R25)", () => {
    const markup = renderToStaticMarkup(
      createElement(ProductivitySeriesChart, { series: SKEWED_SERIES }),
    );

    expect(markup).toMatch(/GitHub/i);
    expect(markup).not.toMatch(/single project's? productivity/i);
    expect(markup).not.toMatch(/same (set of )?projects?/i);
  });
});

describe("ProductivitySeriesChart — legend visibility toggles", () => {
  it("renders every S1-S3 legend entry as a pressed toggle when nothing is hidden", () => {
    const markup = renderToStaticMarkup(
      createElement(ProductivitySeriesChart, { series: SKEWED_SERIES }),
    );

    for (const key of ["linesAdded", "linesDeleted", "netLines"]) {
      expect(markup).toContain(`data-testid="productivity-legend-toggle-${key}"`);
    }
    expect(markup).not.toContain('aria-pressed="false"');
  });

  it("drops the hidden series' bars but keeps its legend entry, marked unpressed, so it can be switched back on", () => {
    const markup = renderToStaticMarkup(
      createElement(ProductivitySeriesChart, {
        series: SKEWED_SERIES,
        hiddenSeries: ["linesDeleted"],
      }),
    );

    const barGroups = markup.match(/class="recharts-layer recharts-bar"/g) ?? [];
    expect(barGroups.length).toBe(2);

    // The entry survives (a payload-driven legend would have dropped it with its bar).
    expect(markup).toContain('data-testid="productivity-legend-toggle-linesDeleted"');
    expect(markup).toContain("Lines deleted (S2)");
    expect((markup.match(/aria-pressed="false"/g) ?? []).length).toBe(1);
    expect(markup).toContain("productivity-chart__legend-toggle--off");
  });

  it("renders no bars but all three toggles when every series is hidden", () => {
    const markup = renderToStaticMarkup(
      createElement(ProductivitySeriesChart, {
        series: SKEWED_SERIES,
        hiddenSeries: ["linesAdded", "linesDeleted", "netLines"],
      }),
    );

    expect(markup.match(/class="recharts-layer recharts-bar"/g)).toBeNull();
    expect((markup.match(/productivity-legend-toggle-/g) ?? []).length).toBe(3);
  });
});

describe("ProductivitySeriesChart — per-series errors (R-partial-failure)", () => {
  it("renders S1-S3 normally and marks a legend entry with an error badge carrying its reason", () => {
    const markup = renderToStaticMarkup(
      createElement(ProductivitySeriesChart, {
        series: SKEWED_SERIES,
        errors: { linesAdded: "boom: github unavailable" },
      }),
    );

    const barGroups = markup.match(/class="recharts-layer recharts-bar"/g) ?? [];
    expect(barGroups.length).toBe(3);

    expect(markup).toContain('data-testid="productivity-legend-error-linesAdded"');
    expect(markup).toContain("boom: github unavailable");
    expect(markup).not.toContain('data-testid="productivity-legend-error-netLines"');
  });
});

describe("TicketsCompletedChart (S4, tickets completed)", () => {
  it("renders one line against a single y-axis, in its own chart", () => {
    const markup = renderToStaticMarkup(
      createElement(TicketsCompletedChart, { series: SKEWED_SERIES }),
    );

    expect(markup).toContain("<svg");
    const yAxisGroups = markup.match(/recharts-yAxis yAxis/g) ?? [];
    expect(yAxisGroups.length).toBe(1);

    const barGroups = markup.match(/class="recharts-layer recharts-bar"/g) ?? [];
    expect(barGroups.length).toBe(1);
  });

  it("gives the ticket count real vertical spread rather than collapsing to the chart floor", () => {
    const markup = renderToStaticMarkup(
      createElement(TicketsCompletedChart, { series: SKEWED_SERIES, height: 200 }),
    );
    const rects = barRectDims(markup);
    expect(rects.length).toBe(SKEWED_SERIES.ticketsCompleted.length);

    const uniqueYs = new Set(rects.map((r) => Math.round(r.y)));
    const uniqueHeights = new Set(rects.map((r) => Math.round(r.height)));
    expect(uniqueYs.size).toBeGreaterThan(1);
    expect(uniqueHeights.size).toBeGreaterThan(1);
  });

  it("labels the chart's scope as Praxis ticket activity", () => {
    const markup = renderToStaticMarkup(
      createElement(TicketsCompletedChart, { series: SKEWED_SERIES }),
    );
    expect(markup).toMatch(/Praxis/i);
  });

  it("never renders a raw ISO-8601 tick label", () => {
    const markup = renderToStaticMarkup(
      createElement(TicketsCompletedChart, { series: SKEWED_SERIES, bucketUnit: "day" }),
    );
    expect(markup).not.toMatch(/2026-07-\d{2}T/);
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

describe("TicketsCompletedChart — S4 instrumentation-date greying (R27)", () => {
  it("greys S4 before the instrumentation date and shows a ticket-history-starts annotation when the range starts before it", () => {
    const markup = renderToStaticMarkup(
      createElement(TicketsCompletedChart, {
        series: TWELVE_MONTH_SERIES,
        instrumentationDate: INSTRUMENTATION_DATE,
      }),
    );

    expect(markup).toContain('data-testid="s4-instrumentation-annotation"');
    expect(markup).toContain("Ticket history starts");
    expect(markup).not.toMatch(/2026-07-25T/);

    // The pre-instrumentation S4 segment is drawn in the grey/dashed style, the
    // post-instrumentation segment in the normal green style — two distinct S4
    // lines, not one.
    expect(markup).toContain("#9ca3af");
    expect(markup).toContain("#16a34a");
  });

  it("does not grey or annotate S4 when the whole window is after the instrumentation date", () => {
    const markup = renderToStaticMarkup(
      createElement(TicketsCompletedChart, {
        series: TWELVE_MONTH_SERIES,
        instrumentationDate: "2025-01-01T00:00:00+00:00",
      }),
    );

    expect(markup).not.toContain('data-testid="s4-instrumentation-annotation"');
    expect(markup).not.toContain("#9ca3af");
  });

  it("does not grey or annotate S4 when no instrumentation date is known", () => {
    const markup = renderToStaticMarkup(
      createElement(TicketsCompletedChart, { series: TWELVE_MONTH_SERIES }),
    );

    expect(markup).not.toContain('data-testid="s4-instrumentation-annotation"');
    expect(markup).not.toContain("#9ca3af");
  });
});
