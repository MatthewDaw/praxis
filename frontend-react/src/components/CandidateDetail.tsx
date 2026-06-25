import type { Candidate } from "../types/candidate";
import type { DataSourceMode } from "../config/dataSource";
import { AuditTimeline } from "./AuditTimeline";
import { ConfidenceBreakdown } from "./ConfidenceBreakdown";
import { ContradictionPanel } from "./ContradictionPanel";
import { MetadataGrid } from "./MetadataGrid";
import { PhoenixTraces } from "./PhoenixTraces";
import { StateBadge } from "./StateBadge";
import { formatCandidateDate } from "../api/candidateModel";
import { formatUtility, parseFactTrust, utilityTone } from "../api/contextClient";

interface CandidateDetailProps {
  candidates: Candidate[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onPromote: (id: string) => Promise<void>;
  onReject: (id: string, reason?: string) => Promise<void>;
  onRefreshCandidate: (id: string) => Promise<void>;
  refreshingId?: string | null;
  onResolve: (
    contradictionId: string,
    resolution: "keep_primary" | "keep_rival",
    keepId: string,
    rivalTitle: string,
  ) => Promise<void>;
  onDelete: (id: string) => Promise<void> | void;
  /**
   * Record an H1 outcome for a fact (POST /facts/{id}/outcome). Optional: only
   * wired in live mode; absent in fixture/offline modes.
   */
  onRecordOutcome?: (id: string, success: boolean) => Promise<void>;
  /** Fact id whose outcome write is in flight (disables its controls). */
  recordingOutcomeId?: string | null;
  /** Current data-source mode — selects Phoenix proxy (live) vs fixture. */
  dataSourceMode?: DataSourceMode;
}

export function CandidateDetail({
  candidates,
  selectedId,
  onSelect,
  onPromote,
  onReject,
  onRefreshCandidate,
  refreshingId,
  onResolve,
  onDelete,
  onRecordOutcome,
  recordingOutcomeId,
  dataSourceMode = "mock",
}: CandidateDetailProps) {
  const detailPanelId = "candidate-detail-panel";

  if (candidates.length === 0) {
    return (
      <section className="detail-panel" id={detailPanelId} aria-labelledby="detail-empty-heading">
        <p className="detail-panel__label" id="detail-empty-heading">
          Candidate detail
        </p>
        <p className="muted">Select a candidate from the list to inspect details.</p>
      </section>
    );
  }

  const idToCandidate = Object.fromEntries(candidates.map((c) => [c.id, c]));
  const activeId =
    selectedId && idToCandidate[selectedId] ? selectedId : candidates[0].id;
  const candidate = idToCandidate[activeId];
  const pipelineExtra = Object.fromEntries(
    Object.entries(candidate.extra).filter(([key]) => key !== "auditTrail"),
  );
  const trust = parseFactTrust(candidate.extra);

  return (
    <section
      className="detail-panel"
      id={detailPanelId}
      aria-labelledby="detail-title"
    >
      <div className="detail-head">
        <div>
          <p className="detail-panel__label">Candidate detail</p>
          <button
            type="button"
            className="btn secondary"
            disabled={refreshingId === candidate.id}
            onClick={() => void onRefreshCandidate(candidate.id)}
            aria-label={`Refresh only ${candidate.title}`}
            title="Refresh only this candidate from the current data source"
          >
            {refreshingId === candidate.id ? "Refreshing item" : "Refresh item"}
          </button>
        </div>
        <label className="detail-select">
          Inspect candidate
          <select
            value={activeId}
            onChange={(event) => onSelect(event.target.value)}
            aria-controls={detailPanelId}
            aria-label="Inspect candidate"
          >
            {candidates.map((row) => (
              <option key={row.id} value={row.id}>
                {row.title}
              </option>
            ))}
          </select>
        </label>
      </div>

      <h2 className="detail-panel__title" id="detail-title">
        {candidate.title}
      </h2>

      <MetadataGrid
        items={[
          {
            label: "State",
            value: <StateBadge state={candidate.state} label={candidate.displayState} />,
          },
          {
            label: "Provenance",
            value: <code className="mono small">{candidate.provenance}</code>,
          },
          {
            label: "Created",
            value: formatCandidateDate(candidate.createdAt),
          },
          {
            label: "Confidence",
            value: <span className="mono">{candidate.confidence.toFixed(2)}</span>,
          },
          ...(trust
            ? [
                {
                  label: "Utility",
                  value: (
                    <span
                      className={`utility-badge utility-badge--${utilityTone(
                        trust.utility,
                        trust.successCount,
                        trust.failureCount,
                      )}`}
                      title="Laplace-smoothed H1 utility"
                    >
                      {formatUtility(trust.utility)}
                      <span className="utility-badge__counts">
                        {" "}
                        ({trust.successCount}✓ / {trust.failureCount}✗)
                      </span>
                    </span>
                  ),
                },
              ]
            : []),
        ]}
      />

      {onRecordOutcome ? (
        <div className="detail-section" aria-labelledby="detail-outcome-heading">
          <h4 id="detail-outcome-heading">Record outcome (H1 trust)</h4>
          <p className="muted small">
            Did acting on this fact succeed? Outcomes feed its Laplace-smoothed utility.
          </p>
          <div className="context-hit__actions">
            <button
              type="button"
              className="btn secondary"
              disabled={recordingOutcomeId === candidate.id}
              onClick={() => void onRecordOutcome(candidate.id, true)}
            >
              Mark worked
            </button>
            <button
              type="button"
              className="btn secondary"
              disabled={recordingOutcomeId === candidate.id}
              onClick={() => void onRecordOutcome(candidate.id, false)}
            >
              Mark failed
            </button>
          </div>
        </div>
      ) : null}

      <div className="detail-section" aria-labelledby="detail-content-heading">
        <h4 id="detail-content-heading">Content</h4>
        <p className="content-body">{candidate.content}</p>
      </div>

      <div className="detail-section" aria-labelledby="detail-confidence-heading">
        <h4 id="detail-confidence-heading">Confidence</h4>
        <ConfidenceBreakdown candidate={candidate} />
      </div>

      <div className="detail-section" aria-labelledby="detail-audit-heading">
        <h4 id="detail-audit-heading">Audit trail</h4>
        {candidate.auditTrail.length > 0 ? (
          <AuditTimeline entries={candidate.auditTrail} />
        ) : (
          <p className="muted">Full audit events arrive from Matthew&apos;s API in live mode.</p>
        )}
      </div>

      <PhoenixTraces candidate={candidate} mode={dataSourceMode} />

      {Object.keys(pipelineExtra).length > 0 ? (
        <details className="detail-section">
          <summary>Additional pipeline fields</summary>
          <pre>{JSON.stringify(pipelineExtra, null, 2)}</pre>
        </details>
      ) : null}

      {candidate.contradictionIds.length > 0 ? (
        <ContradictionPanel
          candidate={candidate}
          peersById={idToCandidate}
          onPromote={onPromote}
          onReject={onReject}
          onResolve={onResolve}
          onDelete={onDelete}
        />
      ) : (
        <p className="status-ok" role="status">
          <span aria-hidden="true">✓</span>
          No contradictions flagged for this candidate.
        </p>
      )}
    </section>
  );
}
