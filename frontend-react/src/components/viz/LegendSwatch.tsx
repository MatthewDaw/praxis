import { stateNodeColors } from "../graph/graphLayout";
import type { CandidateState } from "../../types/candidate";

interface LegendSwatchProps {
  state: CandidateState;
  label?: string;
}

export function LegendSwatch({ state, label }: LegendSwatchProps) {
  const colors = stateNodeColors(state);
  return (
    <span
      className="legend-swatch"
      style={{
        background: colors.bg,
        borderColor: colors.border,
        color: colors.text,
      }}
      title={label ?? state}
    >
      {label ? label.slice(0, 1) : ""}
    </span>
  );
}
