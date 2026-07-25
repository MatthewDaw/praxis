import { useEffect, useState } from "react";
import { getProductivity, type ApiDataProviderAuth } from "../api/apiClient";
import { PRODUCTIVITY_RANGES, type ProductivityRange, type ProductivitySeries } from "../api/contract";
import { ProductivitySeriesChart } from "./viz/ProductivitySeriesChart";

export interface ProductivityPanelProps {
  apiBaseUrl?: string;
  auth?: string | ApiDataProviderAuth;
}

const DEFAULT_RANGE: ProductivityRange = "4weeks";

/** Human-facing labels for the range dropdown, in display order (R16). */
const RANGE_LABELS: Record<ProductivityRange, string> = {
  day: "Day",
  week: "Week",
  "4weeks": "Last 4 weeks",
  "12months": "Last 12 months",
  alltime: "All time",
};

/**
 * The Productivity tab (R15/R16): fetches the owner-gated `GET /productivity`
 * series and renders them as a multi-series dual-axis chart — the
 * lines-of-code series S1-S3 on the left axis, the ticket-count series S4 on
 * its own right axis, so single-digit ticket counts stay legible beside
 * line counts that commonly run in the thousands. A range dropdown (day,
 * week, last 4 weeks, last 12 months, all time) defaults to "last 4 weeks"
 * on first open; changing it re-queries `/productivity` with the matching
 * `range` and re-renders the chart, whose x-axis relabels to the
 * server-reported bucket unit for that range (R8). Reporting is otherwise
 * filter-free by design — the fixed time-bucket boundaries never depend on
 * the candidate search/state filters, so this renders standalone (no
 * FilterBar).
 */
export function ProductivityPanel({ apiBaseUrl, auth }: ProductivityPanelProps) {
  const [range, setRange] = useState<ProductivityRange>(DEFAULT_RANGE);
  const [series, setSeries] = useState<ProductivitySeries | null>(null);
  const [bucketUnit, setBucketUnit] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!apiBaseUrl) {
      setLoading(false);
      return;
    }
    let active = true;
    setLoading(true);
    setError(null);
    getProductivity(apiBaseUrl, range, auth)
      .then((response) => {
        if (active) {
          setSeries(response.series);
          setBucketUnit(response.bucketUnit);
          setLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (active) {
          setError(err instanceof Error ? err.message : "Failed to load productivity data");
          setLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [apiBaseUrl, auth, range]);

  return (
    <section className="productivity-panel" aria-label="Productivity">
      <label className="productivity-panel__range">
        Range
        <select
          aria-label="Productivity date range"
          data-testid="productivity-range-select"
          value={range}
          onChange={(event) => setRange(event.target.value as ProductivityRange)}
        >
          {PRODUCTIVITY_RANGES.map((value) => (
            <option key={value} value={value}>
              {RANGE_LABELS[value]}
            </option>
          ))}
        </select>
      </label>
      {loading ? (
        <p className="muted" data-testid="productivity-loading">
          Loading productivity data…
        </p>
      ) : error ? (
        <p className="productivity-panel__error" data-testid="productivity-error">
          Couldn't load productivity data: {error}
        </p>
      ) : series ? (
        <ProductivitySeriesChart series={series} bucketUnit={bucketUnit} />
      ) : (
        <p className="muted" data-testid="productivity-empty">
          Productivity reporting is coming soon.
        </p>
      )}
    </section>
  );
}
