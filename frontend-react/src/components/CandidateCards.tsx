import type { Candidate } from "../types/candidate";
import { StateBadge } from "./StateBadge";

interface CandidateCardsProps {
  candidates: Candidate[];
  onSelect: (id: string) => void;
}

export function CandidateCards({ candidates, onSelect }: CandidateCardsProps) {
  if (candidates.length === 0) {
    return (
      <p className="info-banner">
        No candidates match the current filter.
      </p>
    );
  }

  return (
    <div className="card-grid">
      {candidates.map((candidate) => (
        <article key={candidate.id} className="candidate-card">
          <div className="card-head">
            <h3>{candidate.title}</h3>
            <StateBadge state={candidate.state} label={candidate.displayState} />
          </div>
          <p className="card-excerpt">{truncate(candidate.content, 180)}</p>
          <div className="inline-progress">
            <div
              className="progress-fill"
              style={{ width: `${Math.round(candidate.confidence * 100)}%` }}
            />
          </div>
          <p className="mono small">{candidate.provenance}</p>
          <button
            type="button"
            className="btn ghost full"
            onClick={() => onSelect(candidate.id)}
          >
            Inspect in detail
          </button>
        </article>
      ))}
    </div>
  );
}

function truncate(text: string, max: number): string {
  if (text.length <= max) {
    return text;
  }
  return `${text.slice(0, max).trim()}…`;
}
