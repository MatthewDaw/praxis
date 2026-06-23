import { useState } from "react";
import {
  EDGE_LEGEND,
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
}

export function GraphLegend({ className }: GraphLegendProps) {
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
            description="Proposed lessons advance through review; decayed items leave the active queue."
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

          <LegendSection title="Relationships">
            <ul className="viz-legend__list">
              {EDGE_LEGEND.map((entry) => (
                <LegendItem
                  key={entry.kind}
                  marker={<LegendEdgeLine entry={entry} />}
                  label={entry.label}
                  description={entry.description}
                />
              ))}
            </ul>
          </LegendSection>

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
