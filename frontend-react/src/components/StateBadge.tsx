import { candidateStateClass } from "../api/candidateModel";
import type { CandidateState } from "../types/candidate";

interface StateBadgeProps {
  state: CandidateState;
  label: string;
}

export function StateBadge({ state, label }: StateBadgeProps) {
  return (
    <span
      className={`state-badge ${candidateStateClass(state)}`}
      aria-label={`State: ${label}`}
    >
      {label}
    </span>
  );
}
