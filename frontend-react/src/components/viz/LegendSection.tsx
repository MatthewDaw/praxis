import type { ReactNode } from "react";

interface LegendSectionProps {
  title: string;
  children: ReactNode;
  description?: string;
}

export function LegendSection({ title, children, description }: LegendSectionProps) {
  return (
    <section className="viz-legend__section">
      <h3 className="viz-legend__section-title">{title}</h3>
      {description ? <p className="viz-legend__desc">{description}</p> : null}
      {children}
    </section>
  );
}
