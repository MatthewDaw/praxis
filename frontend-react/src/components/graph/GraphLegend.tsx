import { useState } from "react";
import type { GraphEdgeKind } from "../../types/graph";
import {
  edgeLegendByKind,
  GRAPH_INTERACTION_LEGEND,
  LIFECYCLE_LEGEND,
  LegendEdgeLine,
  LegendFlowStrip,
  LegendItem,
  LegendSection,
  LegendSwatch,
  VizLegend,
} from "../viz";

interface GraphLegendProps {
  className?: string;
  /** Edge kinds actually present in the graph (ordered) — the only ones listed. */
  edgeKinds: GraphEdgeKind[];
  /** Kinds the viewer has hidden. */
  hiddenKinds: Set<GraphEdgeKind>;
  /** Toggle an edge kind's visibility. */
  onToggleKind: (kind: GraphEdgeKind) => void;
}

export function GraphLegend({
  className,
  edgeKinds,
  hiddenKinds,
  onToggleKind,
}: GraphLegendProps) {
  // Start collapsed so the explainer doesn't cover the graph; expand on demand.
  const [collapsed, setCollapsed] = useState(true);

  return (
    <VizLegend
      ariaLabel="Knowledge graph legend"
      className={className}
      compact
      headerAction={
        <button
          type="button"
          className="viz-legend__toggle"
          aria-expanded={!collapsed}
          aria-controls="graph-legend-body"
          onClick={() => setCollapsed((value) => !value)}
        >
          {collapsed ? "Show" : "Hide"}
        </button>
      }
    >
      {!collapsed ? (
        <div id="graph-legend-body">
          <LegendSection
            title="Human gate flow"
            description="Proposed facts move through review; rejected items leave the approved queue."
          >
            <LegendFlowStrip />
          </LegendSection>

          <LegendSection
            title="Node states"
            description="Node color = lifecycle state; percentage = confidence score."
          >
            <ul className="viz-legend__list">
              {LIFECYCLE_LEGEND.map((entry) => (
                <LegendItem
                  key={entry.state}
                  marker={<LegendSwatch state={entry.state} label={entry.label} />}
                  label={entry.label}
                  description={entry.description}
                />
              ))}
            </ul>
          </LegendSection>

          {edgeKinds.length > 0 ? (
            <LegendSection
              title="Relationships"
              description="Click an edge type to show or hide it on the graph."
            >
              <ul className="viz-legend__list">
                {edgeKinds.map((kind) => {
                  const entry = edgeLegendByKind(kind);
                  if (!entry) {
                    return null;
                  }
                  const shown = !hiddenKinds.has(kind);
                  return (
                    <li key={kind}>
                      <button
                        type="button"
                        className={`legend-edge-toggle${shown ? "" : " is-off"}`}
                        aria-pressed={shown}
                        onClick={() => onToggleKind(kind)}
                      >
                        <span className="viz-legend__marker" aria-hidden="true">
                          <LegendEdgeLine entry={entry} />
                        </span>
                        <span className="viz-legend__item-text">
                          <span className="viz-legend__item-label">{entry.label}</span>
                          <span className="viz-legend__desc">{entry.description}</span>
                        </span>
                        <span className="legend-edge-toggle__state">
                          {shown ? "shown" : "hidden"}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </LegendSection>
          ) : null}

          <LegendSection title="Interaction">
            <ul className="viz-legend__list">
              <LegendItem
                marker={<span className="legend-selection-ring" />}
                label={GRAPH_INTERACTION_LEGEND.label}
                description={GRAPH_INTERACTION_LEGEND.description}
              />
            </ul>
          </LegendSection>
        </div>
      ) : null}
    </VizLegend>
  );
}
