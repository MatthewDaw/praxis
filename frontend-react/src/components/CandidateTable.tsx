import { useState } from "react";
import { nextPromotionState } from "../api/candidateModel";
import type { Candidate } from "../types/candidate";
import { StateBadge } from "./StateBadge";

const LOW_CONFIDENCE_THRESHOLD = 0.5;

interface CandidateTableProps {
  candidates: Candidate[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onPromote: (id: string) => Promise<void>;
  onReject: (id: string, reason?: string) => Promise<void>;
}

export function CandidateTable({
  candidates,
  selectedId,
  onSelect,
  onPromote,
  onReject,
}: CandidateTableProps) {
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [confirmPromote, setConfirmPromote] = useState<string | null>(null);
  const [confirmReject, setConfirmReject] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState("");

  if (candidates.length === 0) {
    return (
      <p className="info-banner">
        No candidates match the current filter. Try clearing search or choosing <strong>All</strong> states.
      </p>
    );
  }

  const selected = candidates.find((c) => c.id === selectedId) ?? candidates[0];

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
    <div className="table-panel">
      <p className="count-line">
        <strong>{candidates.length}</strong> candidates
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Title</th>
              <th>State</th>
              <th>Confidence</th>
              <th>Provenance</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {candidates.map((candidate) => (
              <tr
                key={candidate.id}
                className={candidate.id === selected?.id ? "selected-row" : undefined}
                onClick={() => onSelect(candidate.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onSelect(candidate.id);
                  }
                }}
                tabIndex={0}
                role="button"
                aria-label={`Select ${candidate.title}`}
              >
                <td>{candidate.title}</td>
                <td>
                  <StateBadge state={candidate.state} label={candidate.displayState} />
                </td>
                <td>
                  <div className="inline-progress">
                    <div
                      className="progress-fill"
                      style={{ width: `${Math.round(candidate.confidence * 100)}%` }}
                    />
                  </div>
                  <span className="mono">{candidate.confidence.toFixed(2)}</span>
                </td>
                <td className="mono small">{candidate.provenance}</td>
                <td>{formatDate(candidate.createdAt)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selected ? (
        <div className="action-bar">
          <span>
            Actions for <strong>{selected.title}</strong>
          </span>
          <div className="action-buttons">
            {nextPromotionState(selected.state) ? (
              confirmPromote === selected.id ? (
                <>
                  {selected.confidence < LOW_CONFIDENCE_THRESHOLD ? (
                    <p
                      className="warning-banner"
                      role="alert"
                      aria-live="assertive"
                    >
                      Confidence is {(selected.confidence * 100).toFixed(0)}% (below{" "}
                      {(LOW_CONFIDENCE_THRESHOLD * 100).toFixed(0)}%) — confirm you want to
                      promote a low-confidence lesson.
                    </p>
                  ) : null}
                  <button
                    type="button"
                    className="btn primary"
                    disabled={pendingId === selected.id}
                    onClick={() => void runPromote(selected.id)}
                    aria-label={`Confirm promote ${selected.title}`}
                  >
                    Confirm promote
                  </button>
                  <button
                    type="button"
                    className="btn ghost"
                    onClick={() => setConfirmPromote(null)}
                  >
                    Cancel
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="btn primary"
                  onClick={() => setConfirmPromote(selected.id)}
                  aria-label={`Promote ${selected.title}`}
                  title="Advance proposed → suggested → active"
                >
                  Promote
                </button>
              )
            ) : (
              <button type="button" className="btn" disabled>
                Promote unavailable
              </button>
            )}

            {confirmReject === selected.id ? (
              <>
                <label className="reject-reason">
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
                  className="btn danger"
                  disabled={pendingId === selected.id}
                  onClick={() => void runReject(selected.id)}
                  aria-label={`Confirm reject ${selected.title}`}
                >
                  Confirm reject
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => {
                    setConfirmReject(null);
                    setRejectReason("");
                  }}
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                className="btn danger-outline"
                onClick={() => setConfirmReject(selected.id)}
                aria-label={`Reject ${selected.title}`}
                title="Remove candidate from review queue"
              >
                Reject
              </button>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function formatDate(iso: string): string {
  if (!iso) {
    return "—";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}
