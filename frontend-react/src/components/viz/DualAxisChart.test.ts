import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DualAxisChart } from "./DualAxisChart";

const SAMPLE_DATA = [
  { label: "Mon", primary: 10, secondary: 100 },
  { label: "Tue", primary: 14, secondary: 120 },
  { label: "Wed", primary: 9, secondary: 95 },
];

describe("DualAxisChart", () => {
  it("renders to an SVG chart", () => {
    const markup = renderToStaticMarkup(
      createElement(DualAxisChart, {
        data: SAMPLE_DATA,
        primaryLabel: "Throughput",
        secondaryLabel: "Latency (ms)",
      }),
    );

    expect(markup).toContain("<svg");
    expect(markup).toContain("recharts-surface");
  });

  it("exposes two independent y-axis bindings (left + right)", () => {
    const markup = renderToStaticMarkup(
      createElement(DualAxisChart, {
        data: SAMPLE_DATA,
        primaryLabel: "Throughput",
        secondaryLabel: "Latency (ms)",
      }),
    );

    // recharts renders one "recharts-yAxis yAxis" group per <YAxis>; the
    // component declares two distinct yAxisId bindings ("left", "right"),
    // so exactly two independent axis groups must be present.
    const yAxisGroups = markup.match(/recharts-yAxis yAxis/g) ?? [];
    expect(yAxisGroups.length).toBe(2);
    expect(markup).toContain('orientation="right"');
  });
});
