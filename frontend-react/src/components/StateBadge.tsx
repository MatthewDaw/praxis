import type { CandidateState } from "../types/candidate";
import { candidateStateColor } from "../api/candidateModel";

interface StateBadgeProps {
  state: CandidateState;
  label: string;
}

export function StateBadge({ state, label }: StateBadgeProps) {
  return (
    <span
      className="state-badge"
      style={{ backgroundColor: candidateStateColor(state) }}
    >
      {label}
    </span>
  );
}
