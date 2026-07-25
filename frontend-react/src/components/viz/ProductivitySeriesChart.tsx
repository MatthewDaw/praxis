import { useState } from "react";
import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { Payload } from "recharts/types/component/DefaultLegendContent";
import type { ProductivitySeries } from "../../api/contract";

/**
 * The disclosure content around the chart (D31/R24): static, load-independent
 * caveats always shown in a persistent footnote strip beneath the chart;
 * per-load conditions (rate-limited, N commits unattributed, showing only the
 * first N points, stale/cached-age) that vary response-to-response and sit
 * behind an "ⓘ" affordance instead of permanently occupying the plot area;
 * and the S4 (tickets-completed) series' ticket-history start date, captioned
 * inline beside its own legend entry since it qualifies only that one series.
 */
export interface ProductivityDisclosures {
  staticCaveats: string[];
  perLoadConditions: string[];
  ticketHistoryStart?: string;
}

/** Caveats inherent to how S1-S3 are computed (D5) and how buckets are windowed —
 * true of every load, so these belong in the always-visible footnote strip rather
 * than behind the per-load "ⓘ" affordance. */
export const DEFAULT_STATIC_CAVEATS: string[] = [
  "Lines added/deleted count the default branch only — forks and other branches are excluded.",
  "Bucket boundaries are fixed to America/Denver and never vary by viewer.",
];

const EMPTY_DISCLOSURES: ProductivityDisclosures = {
  staticCaveats: DEFAULT_STATIC_CAVEATS,
  perLoadConditions: [],
};

export interface ProductivitySeriesChartProps {
  series: ProductivitySeries;
  width?: number;
  height?: number;
  disclosures?: ProductivityDisclosures;
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
  disclosures = EMPTY_DISCLOSURES,
}: ProductivitySeriesChartProps) {
  const data = mergeSeries(series);
  const { staticCaveats, perLoadConditions, ticketHistoryStart } = disclosures;
  const [conditionsOpen, setConditionsOpen] = useState(false);

  // Custom legend content so the S4 (tickets-completed) entry can carry its
  // ticket-history-start caption inline, right beside that one legend label —
  // the caption qualifies only S4, so it never lives in the shared footnote
  // strip where it would read as applying to every series.
  const renderLegend = (props: { payload?: Array<Payload> }) => (
    <ul className="productivity-chart__legend">
      {(props.payload ?? []).map((entry) => (
        <li key={String(entry.value)} style={{ color: entry.color }}>
          {entry.value}
          {String(entry.value).includes("S4") && ticketHistoryStart ? (
            <span
              className="productivity-chart__s4-caption"
              data-testid="productivity-s4-caption"
            >
              {" "}
              (ticket history since {ticketHistoryStart})
            </span>
          ) : null}
        </li>
      ))}
    </ul>
  );

  return (
    <div className="productivity-chart">
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
        <Legend content={renderLegend} />
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

      {perLoadConditions.length > 0 ? (
        <div className="productivity-chart__conditions">
          <button
            type="button"
            className="productivity-chart__info-affordance"
            aria-expanded={conditionsOpen}
            aria-label="Conditions affecting this load"
            data-testid="productivity-info-affordance"
            onClick={() => setConditionsOpen((open) => !open)}
          >
            ⓘ
          </button>
          {conditionsOpen ? (
            <ul
              className="productivity-chart__conditions-list"
              data-testid="productivity-conditions-list"
            >
              {perLoadConditions.map((condition) => (
                <li key={condition}>{condition}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {staticCaveats.length > 0 ? (
        <footer
          className="productivity-chart__footnotes"
          data-testid="productivity-footnote-strip"
        >
          <ul>
            {staticCaveats.map((caveat) => (
              <li key={caveat}>{caveat}</li>
            ))}
          </ul>
        </footer>
      ) : null}
    </div>
  );
}
