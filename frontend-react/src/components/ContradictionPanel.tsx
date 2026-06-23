import { useState } from "react";
import { contradictionPairId } from "../api/contract";
import { nextPromotionState } from "../api/candidateModel";
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
  onPromote: (id: string) => Promise<void>;
  onReject: (id: string, reason?: string) => Promise<void>;
  onDefer: (primaryTitle: string, rivalTitle: string) => void;
}

export function ContradictionPanel({
  candidate,
  peersById,
  onResolve,
  onPromote,
  onReject,
  onDefer,
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
            <div className="action-buttons">
              {nextPromotionState(candidate.state) ? (
                <button
                  type="button"
                  className="btn primary"
                  disabled={pending === `${pairId}:approve-current`}
                  onClick={() => {
                    setPending(`${pairId}:approve-current`);
                    void onPromote(candidate.id).finally(() => setPending(null));
                  }}
                >
                  Approve this fact
                </button>
              ) : null}
              {candidate.state !== "decayed" ? (
                <button
                  type="button"
                  className="btn decay"
                  disabled={pending === `${pairId}:reject-current`}
                  onClick={() => {
                    setPending(`${pairId}:reject-current`);
                    void onReject(candidate.id, `Contradicts ${rival.title}`).finally(() =>
                      setPending(null),
                    );
                  }}
                >
                  Reject this fact
                </button>
              ) : null}
              {nextPromotionState(rival.state) ? (
                <button
                  type="button"
                  className="btn primary"
                  disabled={pending === `${pairId}:approve-rival`}
                  onClick={() => {
                    setPending(`${pairId}:approve-rival`);
                    void onPromote(rival.id).finally(() => setPending(null));
                  }}
                >
                  Approve rival
                </button>
              ) : null}
              {rival.state !== "decayed" ? (
                <button
                  type="button"
                  className="btn decay"
                  disabled={pending === `${pairId}:reject-rival`}
                  onClick={() => {
                    setPending(`${pairId}:reject-rival`);
                    void onReject(rival.id, `Contradicted by ${candidate.title}`).finally(() =>
                      setPending(null),
                    );
                  }}
                >
                  Reject rival
                </button>
              ) : null}
              <button
                type="button"
                className="btn primary"
                disabled={pending === pairId}
                onClick={() => {
                  setPending(pairId);
                  void onResolve(pairId, "keep_primary", candidate.id, rival.title).finally(
                    () => setPending(null),
                  );
                }}
              >
                Keep this candidate
              </button>
              <button
                type="button"
                className="btn"
                disabled={pending === pairId}
                onClick={() => {
                  setPending(pairId);
                  void onResolve(pairId, "keep_rival", rival.id, rival.title).finally(
                    () => setPending(null),
                  );
                }}
              >
                Keep {rival.title.length > 28 ? `${rival.title.slice(0, 28)}…` : rival.title}
              </button>
              <button
                type="button"
                className="btn ghost"
                onClick={() => onDefer(candidate.title, rival.title)}
                aria-label={`Defer contradiction between ${candidate.title} and ${rival.title}`}
                title="Leave both candidates in queue for later review"
              >
                Defer
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
