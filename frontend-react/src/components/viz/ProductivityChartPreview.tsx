import { DualAxisChart, type DualAxisPoint } from "./DualAxisChart";

// Illustrative sample series (facts promoted vs. avg review latency) — a
// capability preview for the productivity surface. The node-graph canvas
// cannot render time series; this proves a dual-axis chart can, ahead of the
// real productivity data pipeline landing.
const SAMPLE_SERIES: DualAxisPoint[] = [
  { label: "Mon", primary: 12, secondary: 4.1 },
  { label: "Tue", primary: 18, secondary: 3.6 },
  { label: "Wed", primary: 9, secondary: 5.2 },
  { label: "Thu", primary: 21, secondary: 3.1 },
  { label: "Fri", primary: 15, secondary: 3.8 },
];

/** Preview of the dual-axis chart capability on the context/analytics tab. */
export function ProductivityChartPreview() {
  return (
    <section className="productivity-chart-preview">
      <h3 className="productivity-chart-preview__title">
        Productivity preview (dual y-axis)
      </h3>
      <p className="muted">
        Sample data — facts promoted vs. average review latency, on independent
        y-axes.
      </p>
      <DualAxisChart
        data={SAMPLE_SERIES}
        primaryLabel="Facts promoted"
        secondaryLabel="Avg latency (h)"
      />
    </section>
  );
}
