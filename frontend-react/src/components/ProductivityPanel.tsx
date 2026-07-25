import { useCallback, useEffect, useRef, useState } from "react";
import { getProductivity, type ApiDataProviderAuth } from "../api/apiClient";
import {
  PRODUCTIVITY_RANGES,
  isShortTtlRange,
  type ProductivityKeyStatus,
  type ProductivityRange,
  type ProductivitySeries,
  type ProductivitySeriesErrors,
} from "../api/contract";
import { ProductivityChartSkeleton } from "./viz/ProductivityChartSkeleton";
import {
  DEFAULT_STATIC_CAVEATS,
  ProductivitySeriesChart,
  type ProductivityDisclosures,
} from "./viz/ProductivitySeriesChart";

export interface ProductivityPanelProps {
  apiBaseUrl?: string;
  auth?: string | ApiDataProviderAuth;
  /** Initial selected range (default `"4weeks"`); the panel's range picker changes it from there. */
  initialRange?: ProductivityRange;
}

const DEFAULT_RANGE: ProductivityRange = "4weeks";

// A click within this window of the last accepted Refresh click is ignored
// (leading-edge debounce): ten clicks inside one second collapse to a single
// outbound request.
const REFRESH_DEBOUNCE_MS = 1000;

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

/** True iff every point across every S1-S4 series is exactly zero — a "no activity in this
 * period" response, distinct from a fetch error. The chart still renders (a flat zero line);
 * this only gates the extra caption, never the error styling path. */
function isAllZero(series: ProductivitySeries): boolean {
  const allPoints = [
    ...series.linesAdded,
    ...series.linesDeleted,
    ...series.netLines,
    ...series.ticketsCompleted,
  ];
  return allPoints.every((point) => point.value === 0);
}

/**
 * The Productivity tab (R15/R33): fetches the owner-gated `GET /productivity`
 * series and renders them as a multi-series dual-axis chart — the
 * lines-of-code series S1-S3 on the left axis, the ticket-count series S4 on
 * its own right axis, so single-digit ticket counts stay legible beside
 * line counts that commonly run in the thousands. Reporting is filter-free
 * by design — the fixed time-bucket boundaries never depend on the
 * candidate search/state filters, so this renders standalone (no FilterBar).
 *
 * A Refresh control lets the caller re-pull without waiting out the TTL: it
 * force-fetches for ranges of four weeks or less (short-TTL, cheap to
 * re-derive) and otherwise reuses the long-TTL cache for 12-month/all-time
 * ranges unless the explicit "Force refresh" affordance is checked. Refresh
 * is debounced on the client (leading-edge, `REFRESH_DEBOUNCE_MS`) so a burst
 * of clicks issues exactly one outbound request, and a last-updated label
 * always reflects the served response's `computed_at`.
 */
export function ProductivityPanel({ apiBaseUrl, auth, initialRange }: ProductivityPanelProps) {
  const [range, setRange] = useState<ProductivityRange>(initialRange ?? DEFAULT_RANGE);
  const [forceAffordance, setForceAffordance] = useState(false);
  const [series, setSeries] = useState<ProductivitySeries | null>(null);
  const [instrumentationDate, setInstrumentationDate] = useState<string | null>(null);
  const [disclosures, setDisclosures] = useState<ProductivityDisclosures | undefined>(undefined);
  const [computedAt, setComputedAt] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [rateLimited, setRateLimited] = useState(false);
  const [keyStatus, setKeyStatus] = useState<ProductivityKeyStatus | null>(null);
  const [reposDiscovered, setReposDiscovered] = useState<number | null>(null);
  const [spacesCount, setSpacesCount] = useState<number | null>(null);
  const [seriesErrors, setSeriesErrors] = useState<ProductivitySeriesErrors>({});
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const lastRefreshAtRef = useRef(-Infinity);

  const fetchSeries = useCallback(
    (fetchRange: ProductivityRange, force: boolean) => {
      if (!apiBaseUrl) {
        setLoading(false);
        return;
      }
      let active = true;
      setLoading(true);
      setError(null);
      getProductivity(apiBaseUrl, fetchRange, auth, force)
        .then((response) => {
          if (active) {
            setKeyStatus(response.keyStatus ?? null);
            setSeries(response.keyStatus ? null : response.series);
            setInstrumentationDate(response.s4InstrumentationDate);
            setDisclosures({
              staticCaveats: DEFAULT_STATIC_CAVEATS,
              perLoadConditions: response.truncated
                ? ["Rate-limited or large window — showing a truncated result for this load."]
                : [],
            });
            setComputedAt(response.computedAt || null);
            setStale(Boolean(response.stale));
            setRateLimited(Boolean(response.rateLimited));
            setReposDiscovered(response.reposDiscovered);
            setSpacesCount(response.spacesCount);
            setSeriesErrors(response.errors);
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
    },
    [apiBaseUrl, auth],
  );

  useEffect(() => fetchSeries(range, false), [range, fetchSeries]);

  const handleRefresh = () => {
    const now = Date.now();
    if (now - lastRefreshAtRef.current < REFRESH_DEBOUNCE_MS) {
      return;
    }
    lastRefreshAtRef.current = now;
    fetchSeries(range, isShortTtlRange(range) || forceAffordance);
  };

  // A user with zero discovered GitHub repositories and zero Praxis spaces has
  // connected nothing yet -- their series are all genuinely empty, which would
  // otherwise render as a flat zero line indistinguishable from "connected but
  // did no work" (R20). Show a dedicated first-run message instead of the chart.
  const isFirstRun = reposDiscovered === 0 && spacesCount === 0;

  return (
    <section className="productivity-panel" aria-label="Productivity">
      {/* The controls bar (range picker, refresh, force-affordance) only makes sense once the
          panel actually has -- or could have -- a chart to control. A blocking GitHub key status
          (missing/expired/insufficient_scope) means there is no data and nothing to refresh or
          re-range, so no button/control renders at all (R21) rather than a dead Refresh button
          beside an operator message. */}
      {!loading && !keyStatus ? (
        <div className="productivity-panel__controls">
          <label>
            Range{" "}
            <select
              data-testid="productivity-range"
              value={range}
              onChange={(event) => setRange(event.target.value as ProductivityRange)}
            >
              {PRODUCTIVITY_RANGES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
          {!isShortTtlRange(range) ? (
            <label>
              <input
                type="checkbox"
                data-testid="productivity-force-affordance"
                checked={forceAffordance}
                onChange={(event) => setForceAffordance(event.target.checked)}
              />{" "}
              Force refresh
            </label>
          ) : null}
          <button type="button" data-testid="productivity-refresh" onClick={handleRefresh}>
            Refresh
          </button>
          <span className="productivity-panel__last-updated" data-testid="productivity-last-updated">
            {computedAt ? `Last updated: ${computedAt}` : null}
          </span>
        </div>
      ) : null}
      {loading ? (
        <div data-testid="productivity-loading">
          <ProductivityChartSkeleton />
        </div>
      ) : keyStatus ? (
        <p className="productivity-panel__key-status" data-testid={KEY_STATUS_TEST_IDS[keyStatus]}>
          {KEY_STATUS_MESSAGES[keyStatus]}
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
      ) : series ? (
        <>
          {stale ? (
            <p className="productivity-panel__stale-notice" data-testid="productivity-stale-marker">
              <span className="stale-badge">{rateLimited ? "Rate-limited" : "Stale"}</span>{" "}
              <span data-testid="productivity-computed-age">
                {formatComputedAge(computedAt)}
              </span>
            </p>
          ) : null}
          <ProductivitySeriesChart
            series={series}
            instrumentationDate={instrumentationDate}
            disclosures={disclosures}
            errors={seriesErrors}
          />
          {isAllZero(series) ? (
            <p className="muted" data-testid="productivity-no-activity">
              No activity in this period.
            </p>
          ) : null}
        </>
      ) : (
        <p className="muted" data-testid="productivity-empty">
          Productivity reporting is coming soon.
        </p>
      )}
    </section>
  );
}
