import { useEffect, useState } from "react";
import { getProductivity, type ApiDataProviderAuth } from "../api/apiClient";
import type { ProductivityResponse } from "../api/contract";
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
  const [response, setResponse] = useState<ProductivityResponse | null>(null);
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
      .then((res) => {
        if (active) {
          setResponse(res);
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

  // A user with zero discovered GitHub repositories and zero Praxis spaces has
  // connected nothing yet -- their series are all genuinely empty, which would
  // otherwise render as a flat zero line indistinguishable from "connected but
  // did no work" (R20). Show a dedicated first-run message instead of the chart.
  const isFirstRun =
    response !== null && response.reposDiscovered === 0 && response.spacesCount === 0;

  return (
    <section className="productivity-panel" aria-label="Productivity">
      {loading ? (
        <p className="muted" data-testid="productivity-loading">
          Loading productivity data…
        </p>
      ) : error ? (
        <p className="productivity-panel__error" data-testid="productivity-error">
          Couldn't load productivity data: {error}
        </p>
      ) : isFirstRun ? (
        <p className="muted" data-testid="productivity-first-run">
          Nothing connected yet — link a GitHub repository and a Praxis space to see
          productivity data here.
        </p>
      ) : response ? (
        <ProductivitySeriesChart series={response.series} />
      ) : (
        <p className="muted" data-testid="productivity-empty">
          Productivity reporting is coming soon.
        </p>
      )}
    </section>
  );
}
