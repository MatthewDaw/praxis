import { useState } from "react";
import { contradictionPairId } from "../api/contract";
import type { Candidate } from "../types/candidate";

interface ContradictionPanelProps {
  candidate: Candidate;
  peersById: Record<string, Candidate>;
  onResolve: (
    contradictionId: string,
    resolution: "keep_primary" | "keep_rival",
    keepId: string,
    rivalTitle: string,
  ) => Promise<void>;
  onDefer: (primaryTitle: string, rivalTitle: string) => void;
}

export function ContradictionPanel({
  candidate,
  peersById,
  onResolve,
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
      <h4>Contradictions</h4>
      {rivals.map((rival) => {
        const pairId = contradictionPairId(candidate.id, rival.id);
        return (
          <div key={pairId} className="contradiction-pair">
            <div className="compare-grid">
              <div className="compare-card">
                <strong>This candidate</strong>
                <p>{candidate.content}</p>
                <code>{candidate.provenance}</code>
              </div>
              <div className="compare-card rival">
                <strong>Rival: {rival.title}</strong>
                <p>{rival.content}</p>
                <code>{rival.provenance}</code>
              </div>
            </div>
            <div className="action-buttons">
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
