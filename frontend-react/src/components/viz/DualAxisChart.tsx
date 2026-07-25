import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

export interface DualAxisPoint {
  label: string;
  primary: number;
  secondary: number;
}

export interface DualAxisChartProps {
  data: DualAxisPoint[];
  primaryLabel: string;
  secondaryLabel: string;
  width?: number;
  height?: number;
}

/**
 * A time-series line chart with two independent y-axis scales — "left"
 * bound to `primaryLabel` and "right" bound to `secondaryLabel` — so two
 * metrics with different units/ranges (e.g. throughput vs. latency) can
 * share one x-axis without one flattening the other.
 */
export function DualAxisChart({
  data,
  primaryLabel,
  secondaryLabel,
  width = 480,
  height = 240,
}: DualAxisChartProps) {
  return (
    <ComposedChart width={width} height={height} data={data}>
      <CartesianGrid strokeDasharray="3 3" />
      <XAxis dataKey="label" />
      <YAxis
        yAxisId="left"
        label={{ value: primaryLabel, angle: -90, position: "insideLeft" }}
      />
      <YAxis
        yAxisId="right"
        orientation="right"
        label={{ value: secondaryLabel, angle: 90, position: "insideRight" }}
      />
      <Tooltip />
      <Legend />
      <Line
        yAxisId="left"
        type="monotone"
        dataKey="primary"
        name={primaryLabel}
        stroke="#4f46e5"
        dot={false}
      />
      <Line
        yAxisId="right"
        type="monotone"
        dataKey="secondary"
        name={secondaryLabel}
        stroke="#16a34a"
        dot={false}
      />
    </ComposedChart>
  );
}
