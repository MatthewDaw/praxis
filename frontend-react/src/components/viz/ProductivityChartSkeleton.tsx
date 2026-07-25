/**
 * Placeholder shown while the productivity series is in flight (first open,
 * range change, refresh). Deliberately NOT a chart with empty/zeroed axes —
 * rendering real axes before real data invites a flash of misleading zero
 * values, so this renders shimmering bars instead and carries no chart series
 * path at all.
 */
export function ProductivityChartSkeleton() {
  return (
    <div
      className="productivity-panel__skeleton skeleton-panel"
      data-testid="productivity-skeleton"
      aria-hidden="true"
    >
      <div className="skeleton-line skeleton-line--short" />
      <div className="productivity-panel__skeleton-bars">
        <div className="skeleton-line skeleton-bar" style={{ height: "40%" }} />
        <div className="skeleton-line skeleton-bar" style={{ height: "70%" }} />
        <div className="skeleton-line skeleton-bar" style={{ height: "55%" }} />
        <div className="skeleton-line skeleton-bar" style={{ height: "90%" }} />
        <div className="skeleton-line skeleton-bar" style={{ height: "65%" }} />
      </div>
    </div>
  );
}
