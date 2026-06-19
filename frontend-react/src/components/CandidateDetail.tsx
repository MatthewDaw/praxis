import type { Candidate } from "../types/candidate";
import { ConfidenceBreakdown } from "./ConfidenceBreakdown";
import { ContradictionPanel } from "./ContradictionPanel";
import { StateBadge } from "./StateBadge";

interface CandidateDetailProps {
  candidates: Candidate[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onResolve: (
    contradictionId: string,
    resolution: "keep_primary" | "keep_rival",
    keepId: string,
    rivalTitle: string,
  ) => Promise<void>;
}

export function CandidateDetail({
  candidates,
  selectedId,
  onSelect,
  onResolve,
}: CandidateDetailProps) {
  if (candidates.length === 0) {
    return (
      <section className="detail-panel">
        <h2>Candidate detail</h2>
        <p className="muted">Select a candidate from the list views above.</p>
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

  return (
    <section className="detail-panel">
      <div className="detail-head">
        <h2>Candidate detail</h2>
        <label className="detail-select">
          Inspect candidate
          <select
            value={activeId}
            onChange={(event) => onSelect(event.target.value)}
          >
            {candidates.map((row) => (
              <option key={row.id} value={row.id}>
                {row.title}
              </option>
            ))}
          </select>
        </label>
      </div>

      <h3>{candidate.title}</h3>
      <p>
        <strong>State:</strong>{" "}
        <StateBadge state={candidate.state} label={candidate.displayState} />
      </p>
      <p className="mono small">
        <strong>Provenance:</strong> {candidate.provenance}
      </p>

      <div className="detail-section">
        <h4>Content</h4>
        <p className="content-body">{candidate.content}</p>
      </div>

      <div className="detail-section">
        <h4>Confidence</h4>
        <ConfidenceBreakdown candidate={candidate} />
      </div>

      <div className="detail-section">
        <h4>Audit trail</h4>
        {candidate.auditTrail.length > 0 ? (
          <ul className="audit-list">
            {candidate.auditTrail.map((entry, index) => (
              <li key={`${entry.action}-${index}`}>
                <strong>{entry.action}</strong> · {entry.timestamp} ·{" "}
                <code>{entry.provenance}</code> · <em>{entry.actor}</em>
                {entry.note ? ` — ${entry.note}` : ""}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">
            Created {candidate.createdAt} · Source log line{" "}
            <code>{candidate.provenance}</code>. Full audit events arrive from
            Matthew&apos;s API in live mode.
          </p>
        )}
      </div>

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
          onResolve={onResolve}
        />
      ) : (
        <p className="muted">No contradictions flagged for this candidate.</p>
      )}
    </section>
  );
}
