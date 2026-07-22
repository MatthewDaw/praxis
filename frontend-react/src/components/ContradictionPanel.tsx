import { useState } from "react";
import { contradictionPairId } from "../api/contract";
import { contradictionStatusFor } from "../api/candidateModel";
import type { Candidate } from "../types/candidate";
import { StateBadge } from "./StateBadge";

interface ContradictionPanelProps {
  candidate: Candidate;
  peersById: Record<string, Candidate>;
  onResolve: (
    contradictionId: string,
    resolution: "keep_primary" | "keep_rival",
    keepId: string,
    rivalTitle: string,
  ) => Promise<void>;
  onDelete: (id: string) => Promise<void> | void;
}

export function ContradictionPanel({
  candidate,
  peersById,
  onResolve,
  onDelete,
}: ContradictionPanelProps) {
  const [pending, setPending] = useState<string | null>(null);
  const rivals = candidate.contradictionIds
    .map((id) => peersById[id])
    .filter((rival): rival is Candidate => !!rival);

  if (rivals.length === 0) {
    return (
      <p className="info-banner">
        Contradiction IDs referenced but rival candidates not loaded.
      </p>
    );
  }

  return (
    <div className="contradiction-panel">
      <h4>Facts contradicted by this fact</h4>
      {rivals.map((rival) => {
        const pairId = contradictionPairId(candidate.id, rival.id);
        const status = contradictionStatusFor(candidate, rival.id) ?? "pending";
        return (
          <div key={pairId} className="contradiction-pair">
            <div className="compare-grid">
              <div className="compare-card">
                <strong>This candidate</strong>
                <StateBadge state={candidate.state} label={candidate.displayState} />
                <p>{candidate.content}</p>
                <code>{candidate.provenance}</code>
              </div>
              <div className="compare-card rival">
                <strong>Rival: {rival.title}</strong>
                <StateBadge state={rival.state} label={rival.displayState} />
                <p>{rival.content}</p>
                <code>{rival.provenance}</code>
              </div>
            </div>
            <span className={`contradiction-status contradiction-status--${status}`}>
              {status === "resolved" ? "Resolved" : "Pending"}
            </span>
            <div className="action-buttons">
              {rival.state === "rejected" ? (
                <>
                  <button
                    type="button"
                    className="btn primary"
                    disabled={pending === `${pairId}:approve-rival`}
                    onClick={() => {
                      setPending(`${pairId}:approve-rival`);
                      void onResolve(pairId, "keep_rival", rival.id, rival.title).finally(
                        () => setPending(null),
                      );
                    }}
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    className="btn"
                    disabled={pending === `${pairId}:delete-rival`}
                    onClick={() => {
                      setPending(`${pairId}:delete-rival`);
                      void Promise.resolve(onDelete(rival.id)).finally(() =>
                        setPending(null),
                      );
                    }}
                  >
                    Delete
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="btn decay"
                  disabled={pending === `${pairId}:reject-rival`}
                  onClick={() => {
                    setPending(`${pairId}:reject-rival`);
                    void onResolve(pairId, "keep_primary", candidate.id, rival.title).finally(
                      () => setPending(null),
                    );
                  }}
                >
                  Reject
                </button>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
