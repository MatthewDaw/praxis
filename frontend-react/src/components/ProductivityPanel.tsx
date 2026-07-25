import { useEffect, useState } from "react";
import { getProductivity, type ApiDataProviderAuth } from "../api/apiClient";
import type { ProductivityKeyStatus, ProductivitySeries } from "../api/contract";
import { ProductivitySeriesChart } from "./viz/ProductivitySeriesChart";

export interface ProductivityPanelProps {
  apiBaseUrl?: string;
  auth?: string | ApiDataProviderAuth;
}

const DEFAULT_RANGE = "4weeks" as const;

// Operator-facing copy for each GitHub-key failure the backend can report (R21). Each
// names the specific condition (never a raw 401) and never invites the caller to
// "connect GitHub" -- the token is a backend secret, not something an end user holds
// or can supply, so no such prompt is ever rendered for any of these three states.
const KEY_STATUS_MESSAGES: Record<ProductivityKeyStatus, string> = {
  missing:
    "GitHub token not configured on the backend. Ask an operator to add it (see docs/solutions/conventions/github-token-storage.md).",
  expired:
    "GitHub token was rejected (expired or revoked). Ask an operator to rotate it (see docs/solutions/conventions/github-token-storage.md).",
  insufficient_scope:
    "GitHub token is missing the required Contents: Read permission. Ask an operator to reissue it with that scope.",
};

const KEY_STATUS_TEST_IDS: Record<ProductivityKeyStatus, string> = {
  missing: "productivity-key-status-missing",
  expired: "productivity-key-status-expired",
  insufficient_scope: "productivity-key-status-insufficient-scope",
};

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
  const [keyStatus, setKeyStatus] = useState<ProductivityKeyStatus | null>(null);
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
          setKeyStatus(response.keyStatus ?? null);
          setSeries(response.keyStatus ? null : response.series);
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
      ) : keyStatus ? (
        <p className="productivity-panel__key-status" data-testid={KEY_STATUS_TEST_IDS[keyStatus]}>
          {KEY_STATUS_MESSAGES[keyStatus]}
        </p>
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
