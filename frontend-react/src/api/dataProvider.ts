import type { Candidate, EvalMetrics } from "../types/candidate";

export interface DataProvider {
  listCandidates(state?: string): Promise<Candidate[]>;
  getCandidate(id: string): Promise<Candidate | null>;
  promote(id: string): Promise<Candidate>;
  reject(id: string, reason?: string): Promise<void>;
  resolveContradiction(
    contradictionId: string,
    resolution: "keep_primary" | "keep_rival",
    keepId: string,
  ): Promise<Candidate>;
  getEvalMetrics(): Promise<EvalMetrics>;
}
