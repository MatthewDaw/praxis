import { useEffect, useState } from "react";
import { getProductivity, type ApiDataProviderAuth } from "../api/apiClient";
import type { ProductivityResponse } from "../api/contract";
import { ProductivitySeriesChart } from "./viz/ProductivitySeriesChart";

export interface ProductivityPanelProps {
  apiBaseUrl?: string;
  auth?: string | ApiDataProviderAuth;
}

const DEFAULT_RANGE = "4weeks" as const;

/** Human-readable age of `computedAt` relative to `now` (R22), e.g. "5m old". Empty
 * string for a missing/unparseable timestamp -- callers should skip rendering. */
export function formatComputedAge(computedAt: string | null, now: number = Date.now()): string {
  if (!computedAt) return "";
  const computedMs = Date.parse(computedAt);
  if (Number.isNaN(computedMs)) return "";
  const seconds = Math.max(0, Math.round((now - computedMs) / 1000));
  if (seconds < 60) return `${seconds}s old`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m old`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h old`;
  const days = Math.round(hours / 24);
  return `${days}d old`;
}

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
      .then((result) => {
        if (active) {
          setResponse(result);
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
        <p className="muted" data-testid="productivity-loading">
          Loading productivity data…
        </p>
      ) : error ? (
        <p className="productivity-panel__error" data-testid="productivity-error">
          Couldn't load productivity data: {error}
        </p>
      ) : response ? (
        <>
          {response.stale ? (
            <p className="productivity-panel__stale-notice" data-testid="productivity-stale-marker">
              <span className="stale-badge">
                {response.rateLimited ? "Rate-limited" : "Stale"}
              </span>{" "}
              <span data-testid="productivity-computed-age">
                {formatComputedAge(response.computedAt)}
              </span>
            </p>
          ) : null}
          <ProductivitySeriesChart series={response.series} />
        </>
      ) : (
        <p className="muted" data-testid="productivity-empty">
          Productivity reporting is coming soon.
        </p>
      )}
    </section>
  );
}
