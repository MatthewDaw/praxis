import { useCallback, useEffect, useMemo, useState } from "react";
import { resolveDataProvider } from "../api/providerFactory";
import type { DataProvider } from "../api/dataProvider";
import type { DataSourceConfig } from "../config/dataSource";
import type { Candidate, CandidateWriteInput } from "../types/candidate";
import type { ParsedLogSession } from "../types/transcript";

export interface UseCandidatesOptions {
  config: DataSourceConfig;
  providerOverride?: DataProvider;
  localSession?: ParsedLogSession | null;
}

export function useCandidates(options: UseCandidatesOptions) {
  const { config, providerOverride, localSession } = options;
  const provider = useMemo<DataProvider>(
    () =>
      providerOverride ?? resolveDataProvider(config, localSession),
    [config, localSession, providerOverride],
  );
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastAction, setLastAction] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const rows = await provider.listCandidates();
      setCandidates(rows);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setCandidates([]);
    } finally {
      setLoading(false);
    }
  }, [provider]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    setLastAction(null);
  }, [config]);

  const promote = useCallback(
    async (id: string) => {
      const updated = await provider.promote(id);
      setCandidates((prev) => prev.map((c) => (c.id === id ? updated : c)));
      setLastAction(`Promoted ${updated.title} to ${updated.displayState}.`);
    },
    [provider],
  );

  const reject = useCallback(
    async (id: string, reason?: string) => {
      await provider.reject(id, reason);
      await refresh();
      const note = reason ? ` (reason: ${reason})` : "";
      setLastAction(`Decayed eval ${id}${note}.`);
    },
    [provider, refresh],
  );

  const resolveContradiction = useCallback(
    async (
      contradictionId: string,
      resolution: "keep_primary" | "keep_rival",
      keepId: string,
      rivalTitle: string,
    ) => {
      await provider.resolveContradiction(contradictionId, resolution, keepId);
      await refresh();
      setLastAction(`Resolved contradiction — kept ${keepId} over ${rivalTitle}.`);
    },
    [provider, refresh],
  );

  const createCandidate = useCallback(
    async (input: CandidateWriteInput) => {
      const created = await provider.createCandidate(input);
      await refresh();
      setLastAction(`Added eval "${created.title}".`);
      return created;
    },
    [provider, refresh],
  );

  const updateCandidate = useCallback(
    async (id: string, input: CandidateWriteInput) => {
      const updated = await provider.updateCandidate(id, input);
      setCandidates((prev) => prev.map((c) => (c.id === id ? updated : c)));
      setLastAction(`Updated eval "${updated.title}".`);
      return updated;
    },
    [provider],
  );

  const deleteCandidate = useCallback(
    async (id: string) => {
      const existing = candidates.find((c) => c.id === id);
      await provider.deleteCandidate(id);
      await refresh();
      setLastAction(`Deleted eval "${existing?.title ?? id}".`);
    },
    [provider, refresh, candidates],
  );

  return {
    provider,
    candidates,
    loading,
    error,
    lastAction,
    clearLastAction: () => setLastAction(null),
    refresh,
    promote,
    reject,
    resolveContradiction,
    createCandidate,
    updateCandidate,
    deleteCandidate,
  };
}

export function filterCandidates(
  candidates: Candidate[],
  searchQuery: string,
  stateFilter: string,
): Candidate[] {
  let filtered = candidates;
  if (searchQuery.trim()) {
    const q = searchQuery.trim().toLowerCase();
    filtered = filtered.filter(
      (c) =>
        c.title.toLowerCase().includes(q) || c.content.toLowerCase().includes(q),
    );
  }
  if (stateFilter !== "All") {
    filtered = filtered.filter((c) => c.displayState === stateFilter);
  }
  return filtered;
}
