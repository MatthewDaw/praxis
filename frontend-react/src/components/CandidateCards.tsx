import { useState } from "react";
import { nextPromotionState } from "../api/candidateModel";
import type { Candidate } from "../types/candidate";
import { StateBadge } from "./StateBadge";

const LOW_CONFIDENCE_THRESHOLD = 0.5;

interface CandidateCardsProps {
  candidates: Candidate[];
  onSelect: (id: string) => void;
  onPromote: (id: string) => Promise<void>;
  onReject: (id: string, reason?: string) => Promise<void>;
}

export function CandidateCards({
  candidates,
  onSelect,
  onPromote,
  onReject,
}: CandidateCardsProps) {
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [confirmPromote, setConfirmPromote] = useState<string | null>(null);
  const [confirmReject, setConfirmReject] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState("");

  if (candidates.length === 0) {
    return (
      <p className="info-banner">
        No candidates match the current filter.
      </p>
    );
  }

  async function runPromote(id: string) {
    setPendingId(id);
    try {
      await onPromote(id);
    } finally {
      setPendingId(null);
      setConfirmPromote(null);
    }
  }

  async function runReject(id: string) {
    setPendingId(id);
    try {
      const reason = rejectReason.trim() || undefined;
      await onReject(id, reason);
    } finally {
      setPendingId(null);
      setConfirmReject(null);
      setRejectReason("");
    }
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

          {confirmPromote === candidate.id ? (
            <div className="card-actions">
              {candidate.confidence < LOW_CONFIDENCE_THRESHOLD ? (
                <p className="warning-banner" role="alert" aria-live="assertive">
                  Low confidence ({(candidate.confidence * 100).toFixed(0)}%) — confirm promote?
                </p>
              ) : null}
              <button
                type="button"
                className="btn primary full"
                disabled={pendingId === candidate.id}
                onClick={() => void runPromote(candidate.id)}
              >
                Confirm promote
              </button>
              <button
                type="button"
                className="btn ghost full"
                onClick={() => setConfirmPromote(null)}
              >
                Cancel
              </button>
            </div>
          ) : confirmReject === candidate.id ? (
            <div className="card-actions">
              <label className="reject-reason full">
                Rejection reason (optional)
                <input
                  type="text"
                  value={rejectReason}
                  onChange={(event) => setRejectReason(event.target.value)}
                  aria-label="Rejection reason"
                />
              </label>
              <button
                type="button"
                className="btn danger full"
                disabled={pendingId === candidate.id}
                onClick={() => void runReject(candidate.id)}
              >
                Confirm reject
              </button>
              <button
                type="button"
                className="btn ghost full"
                onClick={() => {
                  setConfirmReject(null);
                  setRejectReason("");
                }}
              >
                Cancel
              </button>
            </div>
          ) : (
            <div className="card-actions">
              <button
                type="button"
                className="btn ghost full"
                onClick={() => onSelect(candidate.id)}
              >
                Inspect in detail
              </button>
              <div className="card-action-row">
                {nextPromotionState(candidate.state) ? (
                  <button
                    type="button"
                    className="btn primary"
                    onClick={() => {
                      setConfirmPromote(candidate.id);
                      setConfirmReject(null);
                    }}
                    aria-label={`Promote ${candidate.title}`}
                  >
                    Promote
                  </button>
                ) : (
                  <button type="button" className="btn" disabled>
                    Promote
                  </button>
                )}
                <button
                  type="button"
                  className="btn danger-outline"
                  onClick={() => {
                    setConfirmReject(candidate.id);
                    setConfirmPromote(null);
                  }}
                  aria-label={`Reject ${candidate.title}`}
                >
                  Reject
                </button>
              </div>
            </div>
          )}
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
