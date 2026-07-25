import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ProductivitySeries } from "../../api/contract";

export interface ProductivitySeriesChartProps {
  series: ProductivitySeries;
  width?: number;
  height?: number;
}

interface ChartRow {
  label: string;
  linesAdded?: number;
  linesDeleted?: number;
  netLines?: number;
  ticketsCompleted?: number;
}

const LEFT_AXIS_KEYS = ["linesAdded", "linesDeleted", "netLines"] as const;

/** Merge the four independently-bucketed S1-S4 series into one row-per-bucket
 * table, keyed by `bucketStart`, so recharts can plot all four lines against
 * a shared x-axis. */
function mergeSeries(series: ProductivitySeries): ChartRow[] {
  const byBucket = new Map<string, ChartRow>();
  const assign = (
    points: ProductivitySeries["linesAdded"],
    key: keyof Omit<ChartRow, "label">,
  ) => {
    for (const point of points) {
      const row = byBucket.get(point.bucketStart) ?? { label: point.bucketStart };
      row[key] = point.value;
      byBucket.set(point.bucketStart, row);
    }
  };
  assign(series.linesAdded, "linesAdded");
  assign(series.linesDeleted, "linesDeleted");
  assign(series.netLines, "netLines");
  assign(series.ticketsCompleted, "ticketsCompleted");
  return Array.from(byBucket.values()).sort((a, b) => a.label.localeCompare(b.label));
}

/**
 * The Productivity panel's chart (R15): the lines-of-code series S1
 * (lines added), S2 (lines deleted) and S3 (net lines) share the LEFT
 * y-axis, while the ticket-count series S4 (tickets completed) gets its
 * OWN independent RIGHT y-axis. S1-S3 commonly run in the thousands while
 * S4 is a single-digit count per bucket — sharing one scale would flatten
 * S4 onto the x-axis, so it is never plotted against the left axis's domain.
 */
export function ProductivitySeriesChart({
  series,
  width = 640,
  height = 320,
}: ProductivitySeriesChartProps) {
  const data = mergeSeries(series);
  return (
    <ComposedChart width={width} height={height} data={data}>
      <CartesianGrid strokeDasharray="3 3" />
      <XAxis dataKey="label" />
      <YAxis
        yAxisId="left"
        label={{ value: "Lines of code", angle: -90, position: "insideLeft" }}
      />
      <YAxis
        yAxisId="right"
        orientation="right"
        allowDecimals={false}
        label={{ value: "Tickets completed", angle: 90, position: "insideRight" }}
      />
      <Tooltip />
      <Legend />
      <Line
        yAxisId="left"
        type="monotone"
        dataKey={LEFT_AXIS_KEYS[0]}
        name="Lines added (S1)"
        stroke="#4f46e5"
        dot={false}
      />
      <Line
        yAxisId="left"
        type="monotone"
        dataKey={LEFT_AXIS_KEYS[1]}
        name="Lines deleted (S2)"
        stroke="#dc2626"
        dot={false}
      />
      <Line
        yAxisId="left"
        type="monotone"
        dataKey={LEFT_AXIS_KEYS[2]}
        name="Net lines (S3)"
        stroke="#0891b2"
        dot={false}
      />
      <Line
        yAxisId="right"
        type="monotone"
        dataKey="ticketsCompleted"
        name="Tickets completed (S4)"
        stroke="#16a34a"
        dot={false}
      />
    </ComposedChart>
  );
}
