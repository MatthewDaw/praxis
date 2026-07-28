import { useState } from "react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { Props as LegendContentProps } from "recharts/types/component/DefaultLegendContent";
import type { ProductivitySeries, ProductivitySeriesErrors } from "../../api/contract";

/**
 * The disclosure content around the chart (D31/R24): static, load-independent
 * caveats always shown in a persistent footnote strip beneath the chart;
 * per-load conditions (rate-limited, N commits unattributed, showing only the
 * first N points, stale/cached-age) that vary response-to-response and sit
 * behind an "ⓘ" affordance instead of permanently occupying the plot area;
 * and per-load conditions that vary response-to-response and sit behind an
 * "ⓘ" affordance instead of permanently occupying the plot area. The S4
 * (tickets-completed) ticket-history-start caption is driven directly by
 * `TicketsCompletedChart`'s `instrumentationDate` prop instead of living
 * here — it qualifies only that chart's own legend entry.
 */
export interface ProductivityDisclosures {
  staticCaveats: string[];
  perLoadConditions: string[];
}

/** Caveats inherent to how S1-S3 are computed (D5/D26/R26) and how buckets are windowed —
 * true of every load, so these belong in the always-visible footnote strip rather
 * than behind the per-load "ⓘ" affordance. */
export const DEFAULT_STATIC_CAVEATS: string[] = [
  "Lines added/deleted count commits to the default branch on GitHub only — forks and other branches are excluded.",
  "Bucket boundaries are fixed to America/Denver and never vary by viewer.",
  "Squash-merged commits are attributed to the merging author, not the writing author.",
  "Private repositories owned by other individuals are excluded.",
];

const EMPTY_DISCLOSURES: ProductivityDisclosures = {
  staticCaveats: DEFAULT_STATIC_CAVEATS,
  perLoadConditions: [],
};

export interface ProductivitySeriesChartProps {
  series: ProductivitySeries;
  /** Per-series failure reasons (e.g. the ticket series S4 errored while the git series
   * S1-S3 succeeded) — the affected legend entry gets an error badge instead of the line
   * silently reading as a flat, indistinguishable zero. */
  errors?: ProductivitySeriesErrors;
  width?: number;
  height?: number;
  disclosures?: ProductivityDisclosures;
  /** The server-chosen bucket width ("hour"/"day"/"week"/"month", R8) — labels the x-axis
   * so switching the range dropdown visibly relabels the chart to match its new buckets. */
  bucketUnit?: string;
  /** Chart heading, defaults to "Lines Of Code" — overridable so a per-repo breakdown chart
   * can reuse this same component with a smaller, repo-scoped title. */
  title?: string;
  /** Series keys currently toggled off in the legend. A hidden key draws no `<Bar>` and its
   * legend entry reads as de-emphasised. Owned by the parent panel, never by this chart, so
   * the aggregate chart and every per-repo chart stay in sync (one click hides everywhere). */
  hiddenSeries?: readonly string[];
  /** Called with the clicked legend entry's series key. Absent (the default) leaves the
   * legend inert, which is what a standalone/preview render wants. */
  onToggleSeries?: (key: string) => void;
}

/** S4 (tickets completed) is charted separately from S1-S3 (lines of code): the two scopes
 * are unrelated data sources (a GitHub repo is never joined to a Praxis project, R25) on
 * wildly different scales (thousands of lines vs. single-digit ticket counts), so sharing
 * one plot area either flattens S4 onto the x-axis or forces a confusing dual-axis read. */
export interface TicketsCompletedChartProps {
  series: ProductivitySeries;
  /** S4's instrumentation-start date (R27/D27), `null`/absent when unknown. When
   * the earliest plotted bucket falls before this date, S4 is split into a greyed
   * pre-instrumentation segment and a normal post-instrumentation segment, with a
   * "ticket history starts <date>" annotation — so missing pre-instrumentation
   * history reads as not-yet-recorded, never as zero tickets completed. */
  instrumentationDate?: string | null;
  errors?: ProductivitySeriesErrors;
  width?: number;
  height?: number;
  bucketUnit?: string;
  /** Chart heading, defaults to "Tickets completed (Praxis)" — overridable so a per-org
   * breakdown chart can reuse this same component with a smaller, org-scoped title. */
  title?: string;
}

const BUCKET_AXIS_LABELS: Record<string, string> = {
  hour: "Hourly buckets",
  day: "Daily buckets",
  week: "Weekly buckets",
  month: "Monthly buckets",
};

function bucketAxisLabel(bucketUnit?: string): string {
  if (!bucketUnit) return "";
  return BUCKET_AXIS_LABELS[bucketUnit] ?? `${bucketUnit} buckets`;
}

/** Human-readable tick label for a bucket's ISO `bucketStart`, shaped by the bucket's own
 * width so the axis reads at the right granularity instead of a raw ISO-8601 string
 * (e.g. "3 PM" for an hourly bucket, "Jul 27" for a daily/weekly one, "Jul 2026" for a
 * monthly one). Falls back to the raw value when it doesn't parse as a date. */
export function formatBucketTick(iso: string, bucketUnit?: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  if (bucketUnit === "hour") {
    return parsed.toLocaleTimeString(undefined, { hour: "numeric" });
  }
  if (bucketUnit === "month") {
    return parsed.toLocaleDateString(undefined, { month: "short", year: "numeric" });
  }
  return parsed.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Human-readable full timestamp (date + time), used wherever a raw ISO-8601 string
 * would otherwise be shown verbatim (tooltips, "Last updated", instrumentation date). */
export function formatFullTimestamp(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Human-readable calendar date (no time), used for the S4 instrumentation-start caption
 * where only the day matters. */
export function formatShortDate(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

interface ChartRow {
  label: string;
  linesAdded?: number;
  linesDeleted?: number;
  netLines?: number;
  ticketsCompleted?: number;
  ticketsCompletedBefore?: number;
  ticketsCompletedAfter?: number;
}

const GREY_STROKE = "#9ca3af";
const TICKETS_STROKE = "#16a34a";

/** Split each row's `ticketsCompleted` into a greyed pre-instrumentation value
 * and a normal post-instrumentation value, compared lexicographically against
 * `instrumentationDate` (both are fixed-format UTC ISO-8601 strings, which sort
 * identically to chronological order — same property migration 0013 relies on). */
function splitByInstrumentationDate(rows: ChartRow[], instrumentationDate: string): ChartRow[] {
  return rows.map((row) => {
    if (row.ticketsCompleted === undefined) return row;
    const isBefore = row.label < instrumentationDate;
    return {
      ...row,
      ticketsCompletedBefore: isBefore ? row.ticketsCompleted : undefined,
      ticketsCompletedAfter: isBefore ? undefined : row.ticketsCompleted,
    };
  });
}

/** The three lines-of-code series, in plot/legend order. This — not recharts' own legend
 * payload — is what the custom legend iterates: a toggled-off series draws no `<Bar>`, so
 * a payload-driven legend would drop the very entry needed to switch it back on. */
export const LINES_OF_CODE_SERIES: readonly {
  key: keyof ProductivitySeries;
  name: string;
  fill: string;
}[] = [
  { key: "linesAdded", name: "Lines added (S1)", fill: "#4f46e5" },
  { key: "linesDeleted", name: "Lines deleted (S2)", fill: "#dc2626" },
  { key: "netLines", name: "Net lines (S3)", fill: "#0891b2" },
];

const LEFT_AXIS_KEYS = LINES_OF_CODE_SERIES.map((s) => s.key);

function toRows(
  series: ProductivitySeries,
  keys: (keyof Omit<ChartRow, "label">)[],
): ChartRow[] {
  const byBucket = new Map<string, ChartRow>();
  const assign = (points: ProductivitySeries["linesAdded"], key: keyof Omit<ChartRow, "label">) => {
    for (const point of points) {
      const row = byBucket.get(point.bucketStart) ?? { label: point.bucketStart };
      row[key] = point.value;
      byBucket.set(point.bucketStart, row);
    }
  };
  const seriesByKey: Record<string, ProductivitySeries["linesAdded"]> = {
    linesAdded: series.linesAdded,
    linesDeleted: series.linesDeleted,
    netLines: series.netLines,
    ticketsCompleted: series.ticketsCompleted,
  };
  for (const key of keys) assign(seriesByKey[key], key);
  return Array.from(byBucket.values()).sort((a, b) => a.label.localeCompare(b.label));
}

const CHART_MARGIN = { top: 12, right: 16, left: 8, bottom: 28 };

/**
 * The Productivity panel's lines-of-code chart (R15): S1 (lines added), S2
 * (lines deleted), S3 (net lines) — the owner's GitHub commit activity.
 *
 * S4 (tickets completed) is a wholly separate, unrelated data source (R25) and
 * is charted in :func:`TicketsCompletedChart` instead of sharing this plot on a
 * second y-axis — the two scopes are never implied to describe one project.
 */
export function ProductivitySeriesChart({
  series,
  errors,
  width = 640,
  height = 280,
  disclosures = EMPTY_DISCLOSURES,
  bucketUnit,
  title = "Lines Of Code",
  hiddenSeries = [],
  onToggleSeries,
}: ProductivitySeriesChartProps) {
  const data = toRows(series, [...LEFT_AXIS_KEYS]);
  const axisLabel = bucketAxisLabel(bucketUnit);
  const { staticCaveats, perLoadConditions } = disclosures;
  const [conditionsOpen, setConditionsOpen] = useState(false);
  const isHidden = (key: string) => hiddenSeries.includes(key);

  const renderLegend = () => (
    <ul className="productivity-chart__legend">
      {LINES_OF_CODE_SERIES.map(({ key, name, fill }) => {
        const hidden = isHidden(key);
        const reason = errors?.[key];
        return (
          <li key={key}>
            <button
              type="button"
              className={
                hidden
                  ? "productivity-chart__legend-toggle productivity-chart__legend-toggle--off"
                  : "productivity-chart__legend-toggle"
              }
              style={hidden ? undefined : { color: fill }}
              aria-pressed={!hidden}
              data-testid={`productivity-legend-toggle-${key}`}
              onClick={() => onToggleSeries?.(key)}
            >
              <span
                className="productivity-chart__legend-swatch"
                style={{ background: fill }}
                aria-hidden="true"
              />
              {name}
            </button>
            {reason && (
              <span
                className="productivity-legend__error-badge"
                data-testid={`productivity-legend-error-${key}`}
                title={reason}
              >
                {" "}⚠ error
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );

  return (
    <div className="productivity-chart">
      <div className="productivity-chart__heading">
        <h3 className="productivity-chart__title">{title}</h3>
        {axisLabel ? <span className="productivity-chart__bucket-caption">{axisLabel}</span> : null}
      </div>
      <ComposedChart
        width={width}
        height={height}
        data={data}
        margin={CHART_MARGIN}
        barCategoryGap={0}
        barGap={0}
      >
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis
          dataKey="label"
          tick={{ fontSize: 12 }}
          tickFormatter={(value: string) => formatBucketTick(value, bucketUnit)}
        />
        <YAxis
          width={64}
          tick={{ fontSize: 12 }}
          label={{
            value: "Lines of code",
            angle: -90,
            position: "insideLeft",
            style: { textAnchor: "middle", fontSize: 12 },
          }}
        />
        <Tooltip labelFormatter={(value: string) => formatBucketTick(value, bucketUnit)} />
        <Legend content={renderLegend} />
        {LINES_OF_CODE_SERIES.filter(({ key }) => !isHidden(key)).map(({ key, name, fill }) => (
          <Bar key={key} dataKey={key} name={name} fill={fill} isAnimationActive={false} />
        ))}
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
            <ul className="productivity-chart__conditions-list" data-testid="productivity-conditions-list">
              {perLoadConditions.map((condition) => (
                <li key={condition}>{condition}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {staticCaveats.length > 0 ? (
        <footer className="productivity-chart__footnotes" data-testid="productivity-footnote-strip">
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

/**
 * The Productivity panel's ticket-completion chart (R15/R25): S4 (tickets
 * completed), the org-wide Praxis ticket-completion count — its own chart,
 * its own axis, its own scale, deliberately separate from the GitHub
 * lines-of-code chart above (see module docstring).
 */
export function TicketsCompletedChart({
  series,
  instrumentationDate,
  errors,
  width = 640,
  height = 200,
  bucketUnit,
  title = "Tickets completed (Praxis)",
}: TicketsCompletedChartProps) {
  const merged = toRows(series, ["ticketsCompleted"]);
  const preInstrumentationWindow =
    !!instrumentationDate && merged.length > 0 && merged[0].label < instrumentationDate;
  const data = preInstrumentationWindow
    ? splitByInstrumentationDate(merged, instrumentationDate)
    : merged;
  const axisLabel = bucketAxisLabel(bucketUnit);
  const reason = errors?.ticketsCompleted;

  const renderLegend = ({ payload }: LegendContentProps) => (
    <ul className="productivity-chart__legend">
      {(payload ?? []).map((entry) => (
        <li key={String(entry.value)} style={{ color: entry.color }}>
          {entry.value}
          {String(entry.value).includes("S4") && instrumentationDate ? (
            <span className="productivity-chart__s4-caption" data-testid="productivity-s4-caption">
              {" "}(ticket history since {formatShortDate(instrumentationDate)})
            </span>
          ) : null}
          {reason && (
            <span
              className="productivity-legend__error-badge"
              data-testid="productivity-legend-error-ticketsCompleted"
              title={reason}
            >
              {" "}⚠ error
            </span>
          )}
        </li>
      ))}
    </ul>
  );

  return (
    <div className="productivity-chart productivity-chart--tickets">
      <div className="productivity-chart__heading">
        <h3 className="productivity-chart__title">{title}</h3>
        {axisLabel ? <span className="productivity-chart__bucket-caption">{axisLabel}</span> : null}
      </div>
      {preInstrumentationWindow && instrumentationDate && (
        <p className="productivity-series-chart__annotation" data-testid="s4-instrumentation-annotation">
          Ticket history starts {formatShortDate(instrumentationDate)}
        </p>
      )}
      <ComposedChart
        width={width}
        height={height}
        data={data}
        margin={CHART_MARGIN}
        barCategoryGap={0}
        barGap={0}
      >
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis
          dataKey="label"
          tick={{ fontSize: 12 }}
          tickFormatter={(value: string) => formatBucketTick(value, bucketUnit)}
        />
        <YAxis
          width={40}
          allowDecimals={false}
          tick={{ fontSize: 12 }}
          label={{
            value: "Tickets",
            angle: -90,
            position: "insideLeft",
            style: { textAnchor: "middle", fontSize: 12 },
          }}
        />
        <Tooltip labelFormatter={(value: string) => formatBucketTick(value, bucketUnit)} />
        <Legend content={renderLegend} />
        {preInstrumentationWindow
          ? [
              <Bar
                key="ticketsCompletedBefore"
                dataKey="ticketsCompletedBefore"
                name="Tickets completed (S4, pre-instrumentation)"
                fill={GREY_STROKE}
                isAnimationActive={false}
              />,
              <Bar
                key="ticketsCompletedAfter"
                dataKey="ticketsCompletedAfter"
                name="Tickets completed (S4)"
                fill={TICKETS_STROKE}
                isAnimationActive={false}
              />,
            ]
          : (
            <Bar
              dataKey="ticketsCompleted"
              name="Tickets completed (S4)"
              fill={TICKETS_STROKE}
              isAnimationActive={false}
            />
          )}
      </ComposedChart>
    </div>
  );
}
