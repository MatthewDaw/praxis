import { useState } from "react";
import { contradictionPairId } from "../api/contract";
import type { Candidate } from "../types/candidate";

export interface ContradictionPair {
  primary: Candidate;
  rival: Candidate;
}

/** One conflicting (subject, attribute) slot with all the facts competing on it. */
export interface ContradictionCluster {
  /** Stable key: the sorted member ids joined by "__". */
  id: string;
  /** Optional slot label, when the backend surfaced it. */
  slot?: { subject: string; attribute: string } | null;
  /** All facts competing on this slot (>= 2). */
  members: Candidate[];
  /** The underlying pairwise edges, so resolution stays per-pair. */
  pairs: ContradictionPair[];
}

/**
 * Collapse the per-candidate contradiction references into a unique set of
 * pairs. A↔B is referenced from both sides; we keep one row per logical pair
 * (canonical key = the two ids sorted) and pick the lexicographically smaller
 * id as `primary` for stable ordering.
 */
export function uniqueContradictionPairs(candidates: Candidate[]): ContradictionPair[] {
  const byId = new Map(candidates.map((c) => [c.id, c]));
  const seen = new Set<string>();
  const pairs: ContradictionPair[] = [];
  for (const candidate of candidates) {
    for (const rivalId of candidate.contradictionIds) {
      const rival = byId.get(rivalId);
      if (!rival) continue;
      const key = [candidate.id, rivalId].sort().join("__");
      if (seen.has(key)) continue;
      seen.add(key);
      const [primary, secondary] =
        candidate.id < rival.id ? [candidate, rival] : [rival, candidate];
      pairs.push({ primary, rival: secondary });
    }
  }
  return pairs;
}

function readSlot(c: Candidate): string | null {
  const raw = (c.extra as Record<string, unknown>)?.slot;
  if (raw && typeof raw === "object") {
    const slot = raw as { subject?: unknown; attribute?: unknown };
    if (slot.subject) {
      return [String(slot.subject), String(slot.attribute ?? "")]
        .filter(Boolean)
        .join(" · ");
    }
  }
  return null;
}

/**
 * Group contradiction pairs into clusters: one cluster per connected component
 * of the contradiction graph, i.e. one item per conflicting slot. A plain
 * two-fact conflict is a cluster of size 2 (no regression). Members are sorted
 * by id for stable ordering.
 */
export function contradictionClusters(candidates: Candidate[]): ContradictionCluster[] {
  const pairs = uniqueContradictionPairs(candidates);
  const byId = new Map(candidates.map((c) => [c.id, c]));

  // Union-find over fact ids.
  const parent = new Map<string, string>();
  const find = (x: string): string => {
    let root = parent.get(x) ?? x;
    while (root !== (parent.get(root) ?? root)) root = parent.get(root) ?? root;
    parent.set(x, root);
    return root;
  };
  const union = (a: string, b: string) => {
    const ra = find(a);
    const rb = find(b);
    if (ra !== rb) parent.set(rb, ra);
  };
  for (const p of pairs) union(p.primary.id, p.rival.id);

  const memberSets = new Map<string, Set<string>>();
  const pairGroups = new Map<string, ContradictionPair[]>();
  for (const p of pairs) {
    const root = find(p.primary.id);
    if (!memberSets.has(root)) memberSets.set(root, new Set());
    const set = memberSets.get(root)!;
    set.add(p.primary.id);
    set.add(p.rival.id);
    if (!pairGroups.has(root)) pairGroups.set(root, []);
    pairGroups.get(root)!.push(p);
  }

  const clusters: ContradictionCluster[] = [];
  for (const [root, ids] of memberSets) {
    const memberIds = Array.from(ids).sort();
    const members = memberIds
      .map((id) => byId.get(id))
      .filter((c): c is Candidate => !!c);
    if (members.length < 2) continue;
    const slot =
      members.map(readSlot).find((s): s is string => !!s) ?? null;
    clusters.push({
      id: memberIds.join("__"),
      slot: slot ? { subject: slot, attribute: "" } : null,
      members,
      pairs: pairGroups.get(root) ?? [],
    });
  }
  clusters.sort((a, b) => a.id.localeCompare(b.id));
  return clusters;
}

interface ContradictionsReviewProps {
  /**
   * Slot-aware clusters, authored by the backend (GET /contradictions) and
   * hydrated against the loaded candidates. The component no longer derives them
   * — clustering is the backend's job so it stays slot-correct (contradiction is
   * not transitive across slots).
   */
  clusters: ContradictionCluster[];
  onResolve: (
    contradictionId: string,
    resolution: "keep_primary" | "keep_rival",
    keepId: string,
    rivalTitle: string,
  ) => Promise<void>;
  /** Resolve with a brand-new, user-authored answer (neither side). */
  onResolveCustom?: (contradictionId: string, customText: string) => Promise<void>;
}

export function ContradictionsReview({
  clusters,
  onResolve,
  onResolveCustom,
}: ContradictionsReviewProps) {
  const [pending, setPending] = useState<string | null>(null);
  // Per-cluster draft text for the "write your own resolution" box.
  const [customDrafts, setCustomDrafts] = useState<Record<string, string>>({});

  if (clusters.length === 0) {
    return (
      <p className="muted">
        No contradictions to review — the knowledge base is internally consistent.
      </p>
    );
  }

  /**
   * Keep one member of the cluster: resolve only the pairs that include the kept
   * fact, keeping it (so it stays active and each of its rivals decays). Pairs
   * between two rivals are skipped — resolving them would re-activate a rival via
   * keep_primary/keep_rival, leaving a contradictory pair live; once both rivals
   * decay, that rival↔rival edge no longer surfaces. Each call hits the existing
   * per-pair resolve endpoint — resolution semantics are unchanged.
   */
  const keepMember = async (cluster: ContradictionCluster, keep: Candidate) => {
    setPending(cluster.id);
    try {
      for (const pair of cluster.pairs) {
        const a = pair.primary;
        const b = pair.rival;
        // Only resolve pairs the kept fact is part of; rival↔rival pairs are left
        // alone (both rivals decay through their pair with the kept fact).
        if (a.id !== keep.id && b.id !== keep.id) continue;
        const pairId = contradictionPairId(a.id, b.id);
        const resolution: "keep_primary" | "keep_rival" =
          keep.id === a.id ? "keep_primary" : "keep_rival";
        const discardedTitle = keep.id === a.id ? b.title : a.title;
        await onResolve(pairId, resolution, keep.id, discardedTitle);
      }
    } finally {
      setPending(null);
    }
  };

  const renderCluster = (cluster: ContradictionCluster) => {
    const busy = pending === cluster.id;
    // Custom resolution settles the whole cluster at once: the cluster id is every
    // member id joined by "__", so the backend rejects all of them and supersedes
    // them with the new fact (not just the first pair).
    const draft = customDrafts[cluster.id] ?? "";
    const submitCustom = () => {
      const text = draft.trim();
      if (!text || !onResolveCustom || cluster.members.length === 0) return;
      setPending(cluster.id);
      void onResolveCustom(cluster.id, text).finally(() => setPending(null));
    };
    return (
      <div key={cluster.id} className="contradiction-pair">
        {cluster.slot?.subject && (
          <p className="muted choice-label">
            Conflict on: {cluster.slot.subject}
            {cluster.slot.attribute ? ` · ${cluster.slot.attribute}` : ""}
          </p>
        )}
        <p className="muted">
          {cluster.members.length} competing facts on this slot — keep one.
        </p>
        <div className="compare-grid">
          {cluster.members.map((member, i) => (
            <div
              key={member.id}
              className={`compare-card${i % 2 === 1 ? " rival" : ""}`}
            >
              <span className="choice-label">Choice {String.fromCharCode(65 + i)}</span>
              <div className="choice-head">
                <strong>{member.title}</strong>
                <span className="muted"> · {member.displayState}</span>
              </div>
              {member.content.trim() !== member.title.trim() && <p>{member.content}</p>}
              <code>{member.provenance}</code>
              <button
                type="button"
                className="btn primary choice-keep"
                disabled={busy}
                onClick={() => void keepMember(cluster, member)}
              >
                Keep this
              </button>
            </div>
          ))}
        </div>
        {onResolveCustom && (
          <div className="custom-resolution">
            <label className="choice-label" htmlFor={`custom-${cluster.id}`}>
              Your own resolution
            </label>
            <p className="muted custom-hint">
              None of these fit? Write a fact that settles the dispute — it replaces
              the conflicting sides.
            </p>
            <textarea
              id={`custom-${cluster.id}`}
              className="custom-input"
              rows={2}
              placeholder="e.g. Run migrations automatically on deploy in staging, but require manual approval in production."
              value={draft}
              disabled={busy}
              onChange={(e) =>
                setCustomDrafts((prev) => ({ ...prev, [cluster.id]: e.target.value }))
              }
            />
            <div className="action-buttons custom-actions">
              <button
                type="button"
                className="btn primary"
                disabled={busy || !draft.trim()}
                onClick={submitCustom}
              >
                Resolve with my answer
              </button>
            </div>
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="contradiction-review">
      <p className="muted">
        {clusters.length} contradiction{clusters.length === 1 ? "" : "s"} awaiting
        review. Keep one fact per slot or resolve with your own answer.
      </p>
      {clusters.map(renderCluster)}
    </div>
  );
}
