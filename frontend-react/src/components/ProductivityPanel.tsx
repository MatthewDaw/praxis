import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { getProductivity, type ApiDataProviderAuth } from "../api/apiClient";
import {
  PRODUCTIVITY_BUCKET_UNITS,
  PRODUCTIVITY_RANGES,
  isShortTtlRange,
  type ProductivityBucketUnit,
  type ProductivityKeyStatus,
  type ProductivityOrgSeries,
  type ProductivityRange,
  type ProductivityRepoSeries,
  type ProductivitySeries,
  type ProductivitySeriesErrors,
  type ProductivitySeriesPoint,
} from "../api/contract";
import { ProductivityChartSkeleton } from "./viz/ProductivityChartSkeleton";
import {
  DEFAULT_STATIC_CAVEATS,
  formatFullTimestamp,
  ProductivitySeriesChart,
  TicketsCompletedChart,
  type ProductivityDisclosures,
} from "./viz/ProductivitySeriesChart";

export interface ProductivityPanelProps {
  apiBaseUrl?: string;
  auth?: string | ApiDataProviderAuth;
  /** Initial selected range (default `"4weeks"`); the panel's range picker changes it from there. */
  initialRange?: ProductivityRange;
}

const DEFAULT_RANGE: ProductivityRange = "4weeks";

/** Default "bin by" bucket width -- day, per the feature ask ("Default is day"). Independent
 * of `range`, which only picks the window span. */
const DEFAULT_BUCKET_UNIT: ProductivityBucketUnit = "day";

/** Human-facing labels for the range dropdown, in display order (R16). */
const RANGE_LABELS: Record<ProductivityRange, string> = {
  day: "Day",
  week: "Week",
  "4weeks": "Last 4 weeks",
  "12months": "Last 12 months",
  alltime: "All time",
};

/** Human-facing labels for the "Bin by" dropdown, in display order. */
const BUCKET_UNIT_LABELS: Record<ProductivityBucketUnit, string> = {
  day: "Day",
  week: "Week",
  month: "Month",
};

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

/** True iff every point across `lists` is exactly zero (an empty list counts as zero) — the
 * "no activity in this window" test. A chart whose own series are all zero conveys nothing
 * but a flat floor, so the panel drops it entirely rather than rendering an empty plot. */
function isAllZero(...lists: ProductivitySeriesPoint[][]): boolean {
  return lists.every((points) => points.every((point) => point.value === 0));
}

/** True iff a repo's (or the aggregate's) S1-S3 lines-of-code series carry any activity. */
function hasLinesActivity(series: Omit<ProductivitySeries, "ticketsCompleted">): boolean {
  return !isAllZero(series.linesAdded, series.linesDeleted, series.netLines);
}

/** Keys of `entries` whose series carry activity this window, sorted for a stable order. */
function activeKeys<T>(entries: Record<string, T>, hasActivity: (value: T) => boolean): string[] {
  return Object.keys(entries)
    .filter((key) => hasActivity(entries[key]))
    .sort();
}

interface BreakdownSectionProps {
  /** Collapsed-section label, e.g. "Lines Of Code by repo". */
  label: string;
  count: number;
  testId: string;
  children: ReactNode;
}

/**
 * One collapsible per-repo/per-org breakdown block. Collapsed by default so the panel
 * opens on its two aggregate charts rather than a wall of mini charts, and built on the
 * same disclosure idiom as the "Reading from extra snapshots" mount switcher: a full-width
 * `<button>` bar carrying `aria-expanded` and a ▸/▾ chevron, with the body simply not
 * rendered while collapsed.
 */
function BreakdownSection({ label, count, testId, children }: BreakdownSectionProps) {
  const [collapsed, setCollapsed] = useState(true);
  return (
    <div className="productivity-panel__breakdown" data-testid={testId}>
      <button
        type="button"
        className="productivity-panel__breakdown-bar"
        onClick={() => setCollapsed((value) => !value)}
        aria-expanded={!collapsed}
        data-testid={`${testId}-toggle`}
        title={collapsed ? "Expand" : "Collapse"}
      >
        <span className="productivity-panel__breakdown-chevron" aria-hidden="true">
          {collapsed ? "▸" : "▾"}
        </span>
        <span className="productivity-panel__breakdown-label">{label}</span>
        <span className="productivity-panel__breakdown-count">{count}</span>
      </button>
      {collapsed ? null : (
        <div className="productivity-panel__breakdown-body" data-testid={`${testId}-body`}>
          {children}
        </div>
      )}
    </div>
  );
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
  const [bucketUnit, setBucketUnit] = useState<ProductivityBucketUnit>(DEFAULT_BUCKET_UNIT);
  const [forceAffordance, setForceAffordance] = useState(false);
  const [series, setSeries] = useState<ProductivitySeries | null>(null);
  const [seriesByRepo, setSeriesByRepo] = useState<Record<string, ProductivityRepoSeries>>({});
  const [seriesByOrg, setSeriesByOrg] = useState<Record<string, ProductivityOrgSeries>>({});
  // Which lines-of-code series are toggled off in the legend. Owned here, in the common
  // parent, and handed to every lines-of-code chart -- aggregate and per-repo alike -- so
  // one legend click hides that series across all of them instead of desyncing per chart.
  const [hiddenSeries, setHiddenSeries] = useState<string[]>([]);
  const [instrumentationDate, setInstrumentationDate] = useState<string | null>(null);
  const [disclosures, setDisclosures] = useState<ProductivityDisclosures | undefined>(undefined);
  const [computedAt, setComputedAt] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [rateLimited, setRateLimited] = useState(false);
  const [keyStatus, setKeyStatus] = useState<ProductivityKeyStatus | null>(null);
  const [reposDiscovered, setReposDiscovered] = useState<number | null>(null);
  const [spacesCount, setSpacesCount] = useState<number | null>(null);
  const [seriesErrors, setSeriesErrors] = useState<ProductivitySeriesErrors>({});
  const [resolvedBucketUnit, setResolvedBucketUnit] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const lastRefreshAtRef = useRef(-Infinity);

  const fetchSeries = useCallback(
    (fetchRange: ProductivityRange, force: boolean, fetchBucketUnit: ProductivityBucketUnit) => {
      if (!apiBaseUrl) {
        setLoading(false);
        return;
      }
      let active = true;
      setLoading(true);
      setError(null);
      getProductivity(apiBaseUrl, fetchRange, auth, force, fetchBucketUnit)
        .then((response) => {
          if (active) {
            setKeyStatus(response.keyStatus ?? null);
            setSeries(response.keyStatus ? null : response.series);
            setSeriesByRepo(response.keyStatus ? {} : response.seriesByRepo);
            setSeriesByOrg(response.keyStatus ? {} : response.seriesByOrg);
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
            setResolvedBucketUnit(response.bucketUnit);
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

  useEffect(
    () => fetchSeries(range, false, bucketUnit),
    [range, bucketUnit, fetchSeries],
  );

  const handleRefresh = () => {
    const now = Date.now();
    if (now - lastRefreshAtRef.current < REFRESH_DEBOUNCE_MS) {
      return;
    }
    lastRefreshAtRef.current = now;
    fetchSeries(range, isShortTtlRange(range) || forceAffordance, bucketUnit);
  };

  const toggleSeries = useCallback((key: string) => {
    setHiddenSeries((hidden) =>
      hidden.includes(key) ? hidden.filter((k) => k !== key) : [...hidden, key],
    );
  }, []);


  // A user with zero discovered GitHub repositories and zero Praxis spaces has
  // connected nothing yet -- their series are all genuinely empty, which would
  // otherwise render as a flat zero line indistinguishable from "connected but
  // did no work" (R20). Show a dedicated first-run message instead of the chart.
  const isFirstRun = reposDiscovered === 0 && spacesCount === 0;

  // A chart whose every bucket is zero says nothing the no-activity caption doesn't say
  // better, so it isn't rendered at all -- aggregate or mini, lines or tickets. A
  // breakdown section with no surviving child chart drops its header along with it.
  const showLines = series !== null && hasLinesActivity(series);
  const showTickets = series !== null && !isAllZero(series.ticketsCompleted);
  const activeRepos = activeKeys(seriesByRepo, hasLinesActivity);
  const activeOrgs = activeKeys(seriesByOrg, (org) => !isAllZero(org.ticketsCompleted));
  const nothingToShow =
    !showLines && !showTickets && activeRepos.length === 0 && activeOrgs.length === 0;

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
                  {RANGE_LABELS[r]}
                </option>
              ))}
            </select>
          </label>
          <label>
            Bin by{" "}
            <select
              data-testid="productivity-bucket-unit"
              value={bucketUnit}
              onChange={(event) => setBucketUnit(event.target.value as ProductivityBucketUnit)}
            >
              {PRODUCTIVITY_BUCKET_UNITS.map((u) => (
                <option key={u} value={u}>
                  {BUCKET_UNIT_LABELS[u]}
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
            {computedAt ? `Last updated: ${formatFullTimestamp(computedAt)}` : null}
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
          {showLines ? (
            <ProductivitySeriesChart
              series={series}
              disclosures={disclosures}
              errors={seriesErrors}
              bucketUnit={resolvedBucketUnit}
              hiddenSeries={hiddenSeries}
              onToggleSeries={toggleSeries}
            />
          ) : null}
          {showTickets ? (
            <TicketsCompletedChart
              series={series}
              instrumentationDate={instrumentationDate}
              errors={seriesErrors}
              bucketUnit={resolvedBucketUnit}
            />
          ) : null}
          {activeRepos.length > 0 ? (
            <BreakdownSection
              label="Lines Of Code by repo"
              count={activeRepos.length}
              testId="productivity-by-repo"
            >
              {activeRepos.map((repo) => (
                <div
                  key={repo}
                  className="productivity-panel__repo-chart"
                  data-testid={`productivity-repo-chart-${repo}`}
                >
                  <ProductivitySeriesChart
                    series={{ ...seriesByRepo[repo], ticketsCompleted: [] }}
                    bucketUnit={resolvedBucketUnit}
                    height={200}
                    title={repo}
                    hiddenSeries={hiddenSeries}
                    onToggleSeries={toggleSeries}
                  />
                </div>
              ))}
            </BreakdownSection>
          ) : null}
          {activeOrgs.length > 0 ? (
            <BreakdownSection
              label="Tickets completed by org"
              count={activeOrgs.length}
              testId="productivity-by-org"
            >
              {activeOrgs.map((orgId) => (
                <div
                  key={orgId}
                  className="productivity-panel__repo-chart"
                  data-testid={`productivity-org-chart-${orgId}`}
                >
                  <TicketsCompletedChart
                    series={{
                      linesAdded: [],
                      linesDeleted: [],
                      netLines: [],
                      ticketsCompleted: seriesByOrg[orgId].ticketsCompleted,
                    }}
                    instrumentationDate={instrumentationDate}
                    bucketUnit={resolvedBucketUnit}
                    height={180}
                    title={seriesByOrg[orgId].name}
                  />
                </div>
              ))}
            </BreakdownSection>
          ) : null}
          {nothingToShow ? (
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
