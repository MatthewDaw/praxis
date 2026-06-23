import type { Candidate, CandidateWriteInput } from "../types/candidate";
import type { KnowledgeGraphSnapshot } from "../types/graph";
import type { ParsedLogSession } from "../types/transcript";

/** A saved copy of the live knowledge graph that can be reloaded on demand. */
export interface Snapshot {
  name: string;
  count: number;
  createdAt: string;
}

export interface DataProvider {
  listCandidates(state?: string): Promise<Candidate[]>;
  getCandidate(id: string): Promise<Candidate | null>;
  promote(id: string): Promise<Candidate>;
  reject(id: string, reason?: string): Promise<void>;
  createCandidate(input: CandidateWriteInput): Promise<Candidate>;
  updateCandidate(id: string, input: CandidateWriteInput): Promise<Candidate>;
  deleteCandidate(id: string): Promise<void>;
  resolveContradiction(
    contradictionId: string,
    resolution: "keep_primary" | "keep_rival",
    keepId: string,
  ): Promise<Candidate>;
  /**
   * Resolve a contradiction with a brand-new, user-authored fact that is neither
   * side. Optional: offline fixture providers may not support it. Returns the
   * newly created candidate (both original sides are decayed server-side).
   */
  resolveContradictionCustom?(
    contradictionId: string,
    customText: string,
  ): Promise<Candidate>;
  getGraph(): Promise<KnowledgeGraphSnapshot>;
  getTranscript(): Promise<ParsedLogSession | null>;
  /** List saved snapshots of the live graph. */
  listSnapshots(): Promise<Snapshot[]>;
  /** Save (create or overwrite) the current live graph under the given name. */
  saveSnapshot(name: string): Promise<Snapshot>;
  /**
   * Load the named snapshot into the live graph.
   * `mode:"replace"` (default) is DESTRUCTIVE: it truncates the whole live graph
   * then inserts the snapshot. `mode:"add"` additively merges the snapshot,
   * replacing only nodes it shares by id and keeping other live facts.
   */
  loadSnapshot(name: string, mode?: "add" | "replace"): Promise<{ loaded: number }>;
  /** Delete a saved snapshot. */
  deleteSnapshot(name: string): Promise<{ deleted: string }>;
}
