import type { CandidateState } from "../../types/candidate";
import type { GraphEdgeKind } from "../../types/graph";

export const FUNNEL_STATES = ["proposed", "suggested", "active", "decayed"] as const;

export type FunnelState = (typeof FUNNEL_STATES)[number];

export interface LifecycleLegendEntry {
  state: CandidateState;
  label: string;
  description: string;
}

export interface EdgeLegendEntry {
  kind: GraphEdgeKind;
  label: string;
  description: string;
  dasharray?: string;
  strokeWidth: number;
  animated?: boolean;
  opacity?: number;
}

export interface FlowStep {
  state: FunnelState;
  label: string;
}

export const LIFECYCLE_LEGEND: LifecycleLegendEntry[] = [
  {
    state: "proposed",
    label: "Proposed",
    description: "Awaiting human review",
  },
  {
    state: "suggested",
    label: "Suggested",
    description: "Promoted once; ready for activation",
  },
  {
    state: "active",
    label: "Active",
    description: "Approved for knowledge injection",
  },
  {
    state: "decayed",
    label: "Decayed",
    description: "Rejected or superseded",
  },
];

export const FLOW_STRIP_STEPS: FlowStep[] = [
  { state: "proposed", label: "Proposed" },
  { state: "suggested", label: "Suggested" },
  { state: "active", label: "Active" },
];

export const FLOW_DECAYED_STEP: FlowStep = {
  state: "decayed",
  label: "Decayed",
};

export const EDGE_LEGEND: EdgeLegendEntry[] = [
  {
    kind: "contradiction",
    label: "Contradiction",
    description: "Conflicting lessons (dashed, animated on canvas)",
    dasharray: "6 4",
    strokeWidth: 1.5,
    animated: true,
  },
  {
    kind: "support",
    label: "Support",
    description: "Reinforcing evidence (solid, bold)",
    strokeWidth: 2.5,
  },
  {
    kind: "similarity",
    label: "Similarity",
    description: "Semantic neighbor (solid, thin)",
    strokeWidth: 1.25,
    opacity: 0.75,
  },
];

export const GRAPH_INTERACTION_LEGEND = {
  label: "Selection ring",
  description: "Blue outline = selected node; click to inspect detail panel",
};

export const FUNNEL_LEGEND_CAPTION =
  "Bar height = candidate count per lifecycle state";

export const EVAL_AXIS_LABELS = {
  y: "Correction rate",
  yHint: "0–1 scale · lower is better",
  x: "Eval session",
  series: "Correction rate per session",
  seriesDescription: "Red gradient bars — declining rate shows compounding improvement",
};

export const EVAL_METRIC_LEGEND = [
  {
    id: "cold",
    label: "Corrections (cold)",
    description: "Baseline without PRAXIS injection",
    markerClass: "eval-legend__marker--cold",
  },
  {
    id: "praxis",
    label: "Corrections (with PRAXIS)",
    description: "After knowledge injection",
    markerClass: "eval-legend__marker--praxis",
  },
  {
    id: "reduction",
    label: "Reduction %",
    description: "Improvement vs cold run",
    markerClass: "eval-legend__marker--reduction",
  },
] as const;

export function edgeLegendByKind(kind: GraphEdgeKind): EdgeLegendEntry | undefined {
  return EDGE_LEGEND.find((entry) => entry.kind === kind);
}

export function funnelStatesMatchConfig(states: readonly string[]): boolean {
  return (
    states.length === FUNNEL_STATES.length &&
    FUNNEL_STATES.every((state, index) => states[index] === state)
  );
}
