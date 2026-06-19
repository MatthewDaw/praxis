import type { Candidate } from "../types/candidate";

interface ConfidenceBreakdownProps {
  candidate: Candidate;
}

export function ConfidenceBreakdown({ candidate }: ConfidenceBreakdownProps) {
  const breakdown = candidate.confidenceBreakdown;
  const pct = Math.round(candidate.confidence * 100);

  return (
    <div className="confidence-block">
      <div className="confidence-summary">
        <span>Aggregate</span>
        <div className="progress-track" aria-label={`Confidence ${candidate.confidence.toFixed(2)}`}>
          <div
            className="progress-fill"
            style={{ width: `${pct}%` }}
          />
        </div>
        <span className="mono">{candidate.confidence.toFixed(2)}</span>
      </div>

      {breakdown ? (
        <div className="confidence-grid">
          <Metric
            label="Frequency"
            value={breakdown.frequency}
            rationale={breakdown.frequencyRationale}
          />
          <Metric
            label="Recency"
            value={breakdown.recency}
            rationale={breakdown.recencyRationale}
          />
          <Metric
            label="Breadth"
            value={breakdown.breadth}
            rationale={breakdown.breadthRationale}
          />
        </div>
      ) : (
        <p className="muted">Detailed breakdown arrives from Matthew&apos;s scoring pipeline.</p>
      )}
    </div>
  );
}

function Metric({
  label,
  value,
  rationale,
}: {
  label: string;
  value: number;
  rationale?: string;
}) {
  const pct = Math.round(value * 100);
  return (
    <div className="metric-card" title={rationale || undefined}>
      <div className="metric-head">
        <strong>{label}</strong>
        <span className="mono">{value.toFixed(2)}</span>
      </div>
      <div className="progress-track">
        <div className="progress-fill subtle" style={{ width: `${pct}%` }} />
      </div>
      {rationale ? <p className="metric-rationale">{rationale}</p> : null}
    </div>
  );
}
