import { useEffect, useState } from "react";
import { getProductivity, type ApiDataProviderAuth } from "../api/apiClient";
import type { ProductivitySeries } from "../api/contract";
import { ProductivityChartSkeleton } from "./viz/ProductivityChartSkeleton";
import { ProductivitySeriesChart } from "./viz/ProductivitySeriesChart";

export interface ProductivityPanelProps {
  apiBaseUrl?: string;
  auth?: string | ApiDataProviderAuth;
}

const DEFAULT_RANGE = "4weeks" as const;

/**
 * The Productivity tab (R15): fetches the owner-gated `GET /productivity`
 * series and renders them as a multi-series dual-axis chart — the
 * lines-of-code series S1-S3 on the left axis, the ticket-count series S4 on
 * its own right axis, so single-digit ticket counts stay legible beside
 * line counts that commonly run in the thousands. Reporting is filter-free
 * by design — the fixed time-bucket boundaries never depend on the
 * candidate search/state filters, so this renders standalone (no FilterBar).
 */
export function ProductivityPanel({ apiBaseUrl, auth }: ProductivityPanelProps) {
  const [series, setSeries] = useState<ProductivitySeries | null>(null);
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
    getProductivity(apiBaseUrl, DEFAULT_RANGE, auth)
      .then((response) => {
        if (active) {
          setSeries(response.series);
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
  }, [apiBaseUrl, auth]);

  return (
    <section className="productivity-panel" aria-label="Productivity">
      {loading ? (
        <div data-testid="productivity-loading">
          <ProductivityChartSkeleton />
        </div>
      ) : error ? (
        <p className="productivity-panel__error" data-testid="productivity-error">
          Couldn't load productivity data: {error}
        </p>
      ) : series ? (
        <ProductivitySeriesChart series={series} />
      ) : (
        <p className="muted" data-testid="productivity-empty">
          Productivity reporting is coming soon.
        </p>
      )}
    </section>
  );
}
