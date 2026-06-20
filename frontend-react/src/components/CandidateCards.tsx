import { useState } from "react";
import {
  formatCandidateDate,
  nextPromotionState,
  promoteUnavailableReason,
} from "../api/candidateModel";
import type { Candidate } from "../types/candidate";
import { EmptyState } from "./ui/EmptyState";
import { StateBadge } from "./StateBadge";

const LOW_CONFIDENCE_THRESHOLD = 0.5;

interface CandidateCardsProps {
  candidates: Candidate[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onPromote: (id: string) => Promise<void>;
  onReject: (id: string, reason?: string) => Promise<void>;
  onEdit: (candidate: Candidate) => void;
  onDelete: (id: string) => Promise<void>;
}

export function CandidateCards({
  candidates,
  selectedId,
  onSelect,
  onPromote,
  onReject,
  onEdit,
  onDelete,
}: CandidateCardsProps) {
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [confirmPromote, setConfirmPromote] = useState<string | null>(null);
  const [confirmReject, setConfirmReject] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState("");

  if (candidates.length === 0) {
    return (
      <EmptyState
        message={
          <>
            No candidates match the current filter. Try clearing search or choosing{" "}
            <strong>All</strong> states.
          </>
        }
      />
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
      setConfirmPromote(null);
      setConfirmReject(null);
      setConfirmDelete(null);
      setRejectReason("");
    }
  }

  async function runDelete(id: string) {
    setPendingId(id);
    try {
      await onDelete(id);
    } finally {
      setPendingId(null);
      setConfirmPromote(null);
      setConfirmReject(null);
      setConfirmDelete(null);
    }
  }

  return (
    <div className="card-grid">
      {candidates.map((candidate) => {
        const nextState = nextPromotionState(candidate.state);
        const promoteBlocked = promoteUnavailableReason(candidate);
        const isSelected = candidate.id === selectedId;

        return (
          <article
            key={candidate.id}
            className={
              isSelected ? "candidate-card candidate-card--selected" : "candidate-card"
            }
          >
            <div className="card-head">
              <h3>{candidate.title}</h3>
              <StateBadge state={candidate.state} label={candidate.displayState} />
            </div>
            <p className="card-excerpt">{candidate.content}</p>
            <div className="inline-progress">
              <div
                className="progress-fill"
                style={{ width: `${Math.round(candidate.confidence * 100)}%` }}
              />
            </div>
            <p className="mono small" title={candidate.provenance}>
              {candidate.provenance}
            </p>
            <p className="small muted">
              Created: {formatCandidateDate(candidate.createdAt)}
            </p>

            {confirmPromote === candidate.id ? (
              <div className="card-actions">
                <p className="info-banner">
                  Promote <strong>{candidate.title}</strong> from{" "}
                  <strong>{candidate.displayState}</strong> to{" "}
                  <strong>{nextState}</strong>?
                </p>
                {candidate.confidence < LOW_CONFIDENCE_THRESHOLD ? (
                  <p className="warning-banner" role="alert" aria-live="assertive">
                    Low confidence ({(candidate.confidence * 100).toFixed(0)}%) — confirm
                    promote?
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
                  Decay reason (optional)
                  <input
                    type="text"
                    value={rejectReason}
                    onChange={(event) => setRejectReason(event.target.value)}
                    aria-label="Decay reason"
                  />
                </label>
                <button
                  type="button"
                  className="btn decay full"
                  disabled={pendingId === candidate.id}
                  onClick={() => void runReject(candidate.id)}
                >
                  Confirm decay
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
            ) : confirmDelete === candidate.id ? (
              <div className="card-actions">
                <p className="warning-banner">
                  Delete <strong>{candidate.title}</strong> permanently?
                </p>
                <button
                  type="button"
                  className="btn delete full"
                  disabled={pendingId === candidate.id}
                  onClick={() => void runDelete(candidate.id)}
                >
                  Confirm delete
                </button>
                <button
                  type="button"
                  className="btn ghost full"
                  onClick={() => setConfirmDelete(null)}
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
                {promoteBlocked ? (
                  <p className="small muted">{promoteBlocked}</p>
                ) : null}
                <div className="card-action-rows">
                  <div className="card-action-row">
                    {nextState ? (
                      <button
                        type="button"
                        className="btn primary"
                        onClick={() => {
                          onSelect(candidate.id);
                          setConfirmPromote(candidate.id);
                          setConfirmReject(null);
                          setConfirmDelete(null);
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
                      className="btn decay"
                      onClick={() => {
                        onSelect(candidate.id);
                        setConfirmReject(candidate.id);
                        setConfirmPromote(null);
                        setConfirmDelete(null);
                      }}
                      aria-label={`Decay ${candidate.title}`}
                    >
                      Decay
                    </button>
                  </div>
                  <div className="card-action-row">
                    <button
                      type="button"
                      className="btn edit"
                      onClick={() => {
                        onSelect(candidate.id);
                        setConfirmPromote(null);
                        setConfirmReject(null);
                        setConfirmDelete(null);
                        onEdit(candidate);
                      }}
                      aria-label={`Edit ${candidate.title}`}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="btn delete"
                      onClick={() => {
                        onSelect(candidate.id);
                        setConfirmDelete(candidate.id);
                        setConfirmPromote(null);
                        setConfirmReject(null);
                      }}
                      aria-label={`Delete ${candidate.title}`}
                    >
                      Delete
                    </button>
                  </div>
                </div>
              </div>
            )}
          </article>
        );
      })}
    </div>
  );
}
