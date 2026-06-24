import { useEffect, useState, type KeyboardEvent, type MouseEvent } from "react";
import {
  canDeleteCandidate,
  formatCandidateDate,
  nextPromotionState,
  promoteUnavailableReason,
} from "../api/candidateModel";
import type { Candidate } from "../types/candidate";
import { EmptyState } from "./ui/EmptyState";
import { PaginationControls } from "./ui/PaginationControls";
import { StateBadge } from "./StateBadge";

const LOW_CONFIDENCE_THRESHOLD = 0.5;
const DEFAULT_PAGE_SIZE = 10;
const PAGE_SIZE_OPTIONS = [10, 25, 50];

/** Topic-cluster label for a candidate, if the define-pass assigned one. */
function clusterLabel(candidate: Candidate): string | null {
  const label = candidate.extra.cluster_label;
  return typeof label === "string" && label.trim() ? label : null;
}

function isInteractiveTarget(target: EventTarget): boolean {
  return (
    target instanceof Element &&
    target.closest("button, input, select, textarea, a, label") !== null
  );
}

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
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const totalPages = Math.max(1, Math.ceil(candidates.length / pageSize));
  const currentPage = Math.min(page, totalPages);

  useEffect(() => {
    if (page > totalPages) {
      setPage(totalPages);
    }
  }, [page, totalPages]);

  useEffect(() => {
    if (!selectedId) {
      return;
    }
    const selectedIndex = candidates.findIndex((candidate) => candidate.id === selectedId);
    if (selectedIndex < 0) {
      return;
    }
    const selectedPage = Math.floor(selectedIndex / pageSize) + 1;
    if (selectedPage !== page) {
      setPage(selectedPage);
    }
  }, [candidates, page, pageSize, selectedId]);

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

  const pageStart = (currentPage - 1) * pageSize;
  const visibleCandidates = candidates.slice(pageStart, pageStart + pageSize);

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

  function handlePageChange(nextPage: number) {
    clearConfirmations();
    setPage(nextPage);
    const nextCandidate = candidates[(nextPage - 1) * pageSize];
    if (nextCandidate) {
      onSelect(nextCandidate.id);
    }
  }

  function handlePageSizeChange(nextPageSize: number) {
    clearConfirmations();
    setPageSize(nextPageSize);
    setPage(1);
    if (candidates[0]) {
      onSelect(candidates[0].id);
    }
  }

  function handleCardClick(event: MouseEvent<HTMLElement>, id: string) {
    if (!isInteractiveTarget(event.target)) {
      onSelect(id);
    }
  }

  function handleCardKeyDown(event: KeyboardEvent<HTMLElement>, id: string) {
    if (event.currentTarget !== event.target) {
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelect(id);
    }
  }

  return (
    <>
      <div className="card-grid">
        {visibleCandidates.map((candidate) => {
        const nextState = nextPromotionState(candidate.state);
        const promoteBlocked = promoteUnavailableReason(candidate);
        const canReject = candidate.state !== "rejected";
        const isSelected = candidate.id === selectedId;

        return (
          <article
            key={candidate.id}
            className={
              isSelected ? "candidate-card candidate-card--selected" : "candidate-card"
            }
            onClick={(event) => handleCardClick(event, candidate.id)}
            onKeyDown={(event) => handleCardKeyDown(event, candidate.id)}
            tabIndex={0}
            role="button"
            aria-pressed={isSelected}
            aria-label={`Inspect ${candidate.title} in detail`}
          >
            <div className="card-head">
              <h3>{candidate.title}</h3>
              <StateBadge state={candidate.state} label={candidate.displayState} />
            </div>
            {clusterLabel(candidate) ? (
              <span className="topic-chip" title="Topic cluster">
                {clusterLabel(candidate)}
              </span>
            ) : null}
            {candidate.content.trim() !== candidate.title.trim() && (
              <p className="card-excerpt">{candidate.content}</p>
            )}
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
                  Approve <strong>{candidate.title}</strong> from{" "}
                  <strong>{candidate.displayState}</strong> to{" "}
                  <strong>Approved</strong>?
                </p>
                {candidate.confidence < LOW_CONFIDENCE_THRESHOLD ? (
                  <p className="warning-banner" role="alert" aria-live="assertive">
                    Low confidence ({(candidate.confidence * 100).toFixed(0)}%) — confirm
                    approve?
                  </p>
                ) : null}
                <button
                  type="button"
                  className="btn primary full"
                  disabled={pendingId === candidate.id}
                  onClick={() => void runPromote(candidate.id)}
                >
                  Confirm approve
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
                  Reject reason (optional)
                  <input
                    type="text"
                    value={rejectReason}
                    onChange={(event) => setRejectReason(event.target.value)}
                    aria-label="Reject reason"
                  />
                </label>
                <button
                  type="button"
                  className="btn decay full"
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
                {promoteBlocked ? (
                  <p className="small muted">{promoteBlocked}</p>
                ) : null}
                <div className="card-action-rows">
                  {nextState || canReject ? (
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
                          aria-label={`Approve ${candidate.title}`}
                        >
                          Approve
                        </button>
                      ) : null}
                      {canReject ? (
                        <button
                          type="button"
                          className="btn decay"
                          onClick={() => {
                            onSelect(candidate.id);
                            setConfirmReject(candidate.id);
                            setConfirmPromote(null);
                            setConfirmDelete(null);
                          }}
                          aria-label={`Reject ${candidate.title}`}
                        >
                          Reject
                        </button>
                      ) : null}
                    </div>
                  ) : null}
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
                    {canDeleteCandidate(candidate) ? (
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
                        title="Remove fact permanently"
                      >
                        Delete
                      </button>
                    ) : null}
                  </div>
                </div>
              </div>
            )}
          </article>
        );
        })}
      </div>
      <PaginationControls
        page={currentPage}
        pageSize={pageSize}
        pageSizeOptions={PAGE_SIZE_OPTIONS}
        totalItems={candidates.length}
        onPageChange={handlePageChange}
        onPageSizeChange={handlePageSizeChange}
      />
    </>
  );
}
