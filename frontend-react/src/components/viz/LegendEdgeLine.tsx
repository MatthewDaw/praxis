import type { EdgeLegendEntry } from "./legendConfig";

interface LegendEdgeLineProps {
  entry: EdgeLegendEntry;
}

export function LegendEdgeLine({ entry }: LegendEdgeLineProps) {
  return (
    <svg
      className={`legend-edge-line${entry.animated ? " legend-edge-line--animated" : ""}`}
      width="40"
      height="12"
      viewBox="0 0 40 12"
      aria-hidden="true"
    >
      <line
        x1="2"
        y1="6"
        x2="38"
        y2="6"
        stroke="var(--ink)"
        strokeWidth={entry.strokeWidth}
        strokeDasharray={entry.dasharray}
        strokeLinecap="round"
        opacity={entry.opacity ?? 1}
      />
    </svg>
  );
}
