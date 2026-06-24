import { Fragment, useState, type MouseEvent } from "react";
import {
  canDeleteCandidate,
  formatCandidateDate,
  nextPromotionState,
} from "../api/candidateModel";
import type { Candidate } from "../types/candidate";
import { EmptyState } from "./ui/EmptyState";
import { StateBadge } from "./StateBadge";

const LOW_CONFIDENCE_THRESHOLD = 0.5;

/** Topic-cluster label for a candidate, if the define-pass assigned one. */
function clusterLabel(candidate: Candidate): string | null {
  const label = candidate.extra.cluster_label;
  return typeof label === "string" && label.trim() ? label : null;
}

interface CandidateTableProps {
  candidates: Candidate[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onPromote: (id: string) => Promise<void>;
  onReject: (id: string, reason?: string) => Promise<void>;
  onEdit: (candidate: Candidate) => void;
  onDelete: (id: string) => Promise<void>;
}

export function CandidateTable({
  candidates,
  selectedId,
  onSelect,
  onPromote,
  onReject,
  onEdit,
  onDelete,
}: CandidateTableProps) {
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
            <strong>all</strong> states.
          </>
        }
      />
    );
  }

  const selected = candidates.find((c) => c.id === selectedId) ?? candidates[0];
  const expandedId = confirmPromote ?? confirmReject ?? confirmDelete;

  function clearConfirmations() {
    setConfirmPromote(null);
    setConfirmReject(null);
    setConfirmDelete(null);
    setRejectReason("");
  }

  async function runPromote(id: string) {
    setPendingId(id);
    try {
      await onPromote(id);
    } finally {
      setPendingId(null);
      clearConfirmations();
    }
  }

  async function runReject(id: string) {
    setPendingId(id);
    try {
      const reason = rejectReason.trim() || undefined;
      await onReject(id, reason);
    } finally {
      setPendingId(null);
      clearConfirmations();
    }
  }

  async function runDelete(id: string) {
    setPendingId(id);
    try {
      await onDelete(id);
    } finally {
      setPendingId(null);
      clearConfirmations();
    }
  }

  function handlePromoteClick(event: MouseEvent, candidate: Candidate) {
    event.stopPropagation();
    onSelect(candidate.id);
    if (nextPromotionState(candidate.state)) {
      setConfirmPromote(candidate.id);
      setConfirmReject(null);
      setConfirmDelete(null);
      setRejectReason("");
    }
  }

  function handleRejectClick(event: MouseEvent, candidate: Candidate) {
    event.stopPropagation();
    onSelect(candidate.id);
    setConfirmReject(candidate.id);
    setConfirmPromote(null);
    setConfirmDelete(null);
  }

  function handleEditClick(event: MouseEvent, candidate: Candidate) {
    event.stopPropagation();
    onSelect(candidate.id);
    clearConfirmations();
    onEdit(candidate);
  }

  function handleDeleteClick(event: MouseEvent, candidate: Candidate) {
    event.stopPropagation();
    if (!canDeleteCandidate(candidate)) {
      return;
    }
    onSelect(candidate.id);
    setConfirmDelete(candidate.id);
    setConfirmPromote(null);
    setConfirmReject(null);
  }

  function renderConfirmRow(candidate: Candidate) {
    const nextState = nextPromotionState(candidate.state);
    const isPromote = confirmPromote === candidate.id;
    const isReject = confirmReject === candidate.id;
    const isDelete = confirmDelete === candidate.id;

    return (
      <tr className="row-expand">
        <td colSpan={5}>
          {isPromote && nextState ? (
            <>
              <p className="info-banner">
                Approve <strong>{candidate.title}</strong> from{" "}
                <strong>{candidate.displayState}</strong> to{" "}
                <strong>Approved</strong>?
              </p>
              {candidate.confidence < LOW_CONFIDENCE_THRESHOLD ? (
                <p className="warning-banner" role="alert" aria-live="assertive">
                  Confidence is {(candidate.confidence * 100).toFixed(0)}% (below{" "}
                  {(LOW_CONFIDENCE_THRESHOLD * 100).toFixed(0)}%) — confirm you want to
                  approve a low-confidence lesson.
                </p>
              ) : null}
              <div className="action-buttons">
                <button
                  type="button"
                  className="btn primary"
                  disabled={pendingId === candidate.id}
                  onClick={() => void runPromote(candidate.id)}
                  aria-label={`Confirm approve ${candidate.title}`}
                >
                  Confirm approve
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
                Reject reason (optional)
                <input
                  type="text"
                  value={rejectReason}
                  onChange={(event) => setRejectReason(event.target.value)}
                  aria-label="Reject reason"
                />
              </label>
              <div className="action-buttons">
                <button
                  type="button"
                  className="btn decay"
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
          {isDelete ? (
            <>
              <p className="warning-banner" role="alert">
                Delete <strong>{candidate.title}</strong> permanently? This removes the fact
                from the review queue.
              </p>
              <div className="action-buttons">
                <button
                  type="button"
                  className="btn delete"
                  disabled={pendingId === candidate.id}
                  onClick={() => void runDelete(candidate.id)}
                  aria-label={`Confirm delete ${candidate.title}`}
                >
                  Confirm delete
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => setConfirmDelete(null)}
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
              <th scope="col">Topic</th>
              <th scope="col">State</th>
              <th scope="col">Created</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {candidates.map((candidate) => {
              const isSelected = candidate.id === selected?.id;
              const canPromote = !!nextPromotionState(candidate.state);
              const canReject = candidate.state !== "rejected";

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
                    <td>
                      <span className="title-cell" title={candidate.title}>
                        {candidate.title}
                      </span>
                    </td>
                    <td>
                      {clusterLabel(candidate) ? (
                        <span className="topic-chip" title="Topic cluster">
                          {clusterLabel(candidate)}
                        </span>
                      ) : (
                        <span className="muted small">—</span>
                      )}
                    </td>
                    <td>
                      <StateBadge state={candidate.state} label={candidate.displayState} />
                    </td>
                    <td>{formatCandidateDate(candidate.createdAt)}</td>
                    <td className="actions-cell">
                      <div className="actions-cell__group">
                        {canPromote || canReject ? (
                          <div className="actions-cell__row">
                            {canPromote ? (
                              <button
                                type="button"
                                className="btn primary"
                                onClick={(event) => handlePromoteClick(event, candidate)}
                                aria-label={`Approve ${candidate.title}`}
                                title="Approve proposed fact"
                              >
                                Approve
                              </button>
                            ) : null}
                            {canReject ? (
                              <button
                                type="button"
                                className="btn decay"
                                onClick={(event) => handleRejectClick(event, candidate)}
                                aria-label={`Reject ${candidate.title}`}
                                title="Reject this fact"
                              >
                                Reject
                              </button>
                            ) : null}
                          </div>
                        ) : null}
                        <div className="actions-cell__row">
                          <button
                            type="button"
                            className="btn edit"
                            onClick={(event) => handleEditClick(event, candidate)}
                            aria-label={`Edit ${candidate.title}`}
                          >
                            Edit
                          </button>
                          {canDeleteCandidate(candidate) ? (
                            <button
                              type="button"
                              className="btn delete"
                              onClick={(event) => handleDeleteClick(event, candidate)}
                              aria-label={`Delete ${candidate.title}`}
                              title="Remove fact permanently"
                            >
                              Delete
                            </button>
                          ) : null}
                        </div>
                      </div>
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
