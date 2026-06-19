import type { ReactNode } from "react";

interface MetadataItem {
  label: string;
  value: ReactNode;
}

interface MetadataGridProps {
  items: MetadataItem[];
}

export function MetadataGrid({ items }: MetadataGridProps) {
  return (
    <dl className="metadata-grid">
      {items.map((item) => (
        <div key={item.label} className="metadata-item">
          <dt className="metadata-item__label">{item.label}</dt>
          <dd className="metadata-item__value">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}
