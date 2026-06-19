import type { ReactNode } from "react";

interface LegendItemProps {
  marker: ReactNode;
  label: string;
  description?: string;
}

export function LegendItem({ marker, label, description }: LegendItemProps) {
  return (
    <li className="viz-legend__item">
      <span className="viz-legend__marker" aria-hidden="true">
        {marker}
      </span>
      <span className="viz-legend__item-text">
        <span className="viz-legend__item-label">{label}</span>
        {description ? (
          <span className="viz-legend__desc">{description}</span>
        ) : null}
      </span>
    </li>
  );
}
