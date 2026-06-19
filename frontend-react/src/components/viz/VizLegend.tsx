import type { ReactNode } from "react";

interface VizLegendProps {
  title?: string;
  ariaLabel: string;
  compact?: boolean;
  className?: string;
  headerAction?: ReactNode;
  children: ReactNode;
}

export function VizLegend({
  title = "Legend",
  ariaLabel,
  compact = false,
  className,
  headerAction,
  children,
}: VizLegendProps) {
  const classes = ["viz-legend", compact ? "viz-legend--compact" : "", className]
    .filter(Boolean)
    .join(" ");

  return (
    <aside className={classes} role="region" aria-label={ariaLabel}>
      <div className="viz-legend__header">
        <p className="viz-legend__title">{title}</p>
        {headerAction}
      </div>
      <div className="viz-legend__body">{children}</div>
    </aside>
  );
}
