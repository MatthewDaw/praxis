import { useCallback, useEffect, useMemo, useState } from "react";
import { getDataProvider } from "../api/mockProvider";
import type { DataProvider } from "../api/dataProvider";
import type { Candidate } from "../types/candidate";

export function useCandidates() {
  const provider = useMemo<DataProvider>(() => getDataProvider(), []);
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
      setLastAction(`Rejected candidate ${id}.`);
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
