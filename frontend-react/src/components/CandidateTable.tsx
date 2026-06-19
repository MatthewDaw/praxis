import { Fragment, useState, type MouseEvent } from "react";
import {
  formatCandidateDate,
  nextPromotionState,
  promoteUnavailableReason,
} from "../api/candidateModel";
import type { Candidate } from "../types/candidate";
import { EmptyState } from "./ui/EmptyState";
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

  const selected = candidates.find((c) => c.id === selectedId) ?? candidates[0];
  const expandedId = confirmPromote ?? confirmReject;

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

  function handlePromoteClick(event: MouseEvent, candidate: Candidate) {
    event.stopPropagation();
    onSelect(candidate.id);
    if (nextPromotionState(candidate.state)) {
      setConfirmPromote(candidate.id);
      setConfirmReject(null);
      setRejectReason("");
    }
  }

  function handleRejectClick(event: MouseEvent, candidate: Candidate) {
    event.stopPropagation();
    onSelect(candidate.id);
    setConfirmReject(candidate.id);
    setConfirmPromote(null);
  }

  function renderConfirmRow(candidate: Candidate) {
    const nextState = nextPromotionState(candidate.state);
    const isPromote = confirmPromote === candidate.id;
    const isReject = confirmReject === candidate.id;

    return (
      <tr className="row-expand">
        <td colSpan={6}>
          {isPromote && nextState ? (
            <>
              <p className="info-banner">
                Promote <strong>{candidate.title}</strong> from{" "}
                <strong>{candidate.displayState}</strong> to{" "}
                <strong>{nextState}</strong>?
              </p>
              {candidate.confidence < LOW_CONFIDENCE_THRESHOLD ? (
                <p className="warning-banner" role="alert" aria-live="assertive">
                  Confidence is {(candidate.confidence * 100).toFixed(0)}% (below{" "}
                  {(LOW_CONFIDENCE_THRESHOLD * 100).toFixed(0)}%) — confirm you want to
                  promote a low-confidence lesson.
                </p>
              ) : null}
              <div className="action-buttons">
                <button
                  type="button"
                  className="btn primary"
                  disabled={pendingId === candidate.id}
                  onClick={() => void runPromote(candidate.id)}
                  aria-label={`Confirm promote ${candidate.title}`}
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
              </div>
            </>
          ) : null}
          {isReject ? (
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
              <div className="action-buttons">
                <button
                  type="button"
                  className="btn danger"
                  disabled={pendingId === candidate.id}
                  onClick={() => void runReject(candidate.id)}
                  aria-label={`Confirm reject ${candidate.title}`}
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
              </div>
            </>
          ) : null}
        </td>
      </tr>
    );
  }

  return (
    <div className="table-panel">
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Title</th>
              <th scope="col">State</th>
              <th scope="col" className="col-numeric">
                Confidence
              </th>
              <th scope="col">Provenance</th>
              <th scope="col">Created</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {candidates.map((candidate) => {
              const isSelected = candidate.id === selected?.id;
              const promoteBlocked = promoteUnavailableReason(candidate);
              const canPromote = !!nextPromotionState(candidate.state);

              return (
                <Fragment key={candidate.id}>
                  <tr
                    className={isSelected ? "row-selected" : undefined}
                    onClick={() => onSelect(candidate.id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        onSelect(candidate.id);
                      }
                    }}
                    tabIndex={0}
                    role="button"
                    aria-selected={isSelected}
                    aria-label={`Select ${candidate.title}`}
                  >
                    <td>{candidate.title}</td>
                    <td>
                      <StateBadge state={candidate.state} label={candidate.displayState} />
                    </td>
                    <td className="col-numeric">
                      <span className="confidence-cell">
                        <span className="inline-progress">
                          <span
                            className="progress-fill"
                            style={{ width: `${Math.round(candidate.confidence * 100)}%` }}
                          />
                        </span>
                        <span className="mono">{candidate.confidence.toFixed(2)}</span>
                      </span>
                    </td>
                    <td
                      className="mono small provenance-cell"
                      title={candidate.provenance}
                    >
                      {candidate.provenance}
                    </td>
                    <td>{formatCandidateDate(candidate.createdAt)}</td>
                    <td className="actions-cell">
                      {canPromote ? (
                        <button
                          type="button"
                          className="btn primary"
                          onClick={(event) => handlePromoteClick(event, candidate)}
                          aria-label={`Promote ${candidate.title}`}
                          title="Advance proposed → suggested → active"
                        >
                          Promote
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="btn"
                          disabled
                          title={promoteBlocked ?? undefined}
                          onClick={(event) => event.stopPropagation()}
                        >
                          Promote
                        </button>
                      )}
                      <button
                        type="button"
                        className="btn danger-outline"
                        onClick={(event) => handleRejectClick(event, candidate)}
                        aria-label={`Reject ${candidate.title}`}
                        title="Remove candidate from review queue"
                      >
                        Reject
                      </button>
                    </td>
                  </tr>
                  {expandedId === candidate.id ? renderConfirmRow(candidate) : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
