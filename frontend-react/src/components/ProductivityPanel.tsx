/**
 * Placeholder panel for the Productivity tab. Reporting is filter-free by
 * design — the fixed time-bucket boundaries it will show never depend on the
 * candidate search/state filters, so this renders standalone (no FilterBar).
 */
export function ProductivityPanel() {
  return (
    <section className="productivity-panel" aria-label="Productivity">
      <p className="muted">Productivity reporting is coming soon.</p>
    </section>
  );
}
