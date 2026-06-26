import { useCallback, useEffect, useMemo, useState } from "react";
import { resolveDataProvider } from "../api/providerFactory";
import type { ApiDataProviderAuth } from "../api/apiClient";
import type { DataProvider } from "../api/dataProvider";
import type { DataSourceConfig } from "../config/dataSource";
import type { Candidate, CandidateWriteInput } from "../types/candidate";
import type { ParsedLogSession } from "../types/transcript";

export interface UseCandidatesOptions {
  config: DataSourceConfig;
  providerOverride?: DataProvider;
  localSession?: ParsedLogSession | null;
  auth?: ApiDataProviderAuth;
}

export function useCandidates(options: UseCandidatesOptions) {
  const { config, providerOverride, localSession, auth } = options;
  const getToken = auth?.getToken;
  const orgId = auth?.orgId;
  const spaceId = auth?.spaceId;
  const provider = useMemo<DataProvider>(
    () =>
      providerOverride ??
      resolveDataProvider(
        config,
        localSession,
        getToken ? { getToken, orgId, spaceId } : undefined,
      ),
    [config, localSession, providerOverride, getToken, orgId, spaceId],
  );
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastAction, setLastAction] = useState<string | null>(null);

  const applyCandidate = useCallback((candidate: Candidate) => {
    setCandidates((prev) => {
      const found = prev.some((row) => row.id === candidate.id);
      if (!found) {
        return [...prev, candidate];
      }
      return prev.map((row) => (row.id === candidate.id ? candidate : row));
    });
  }, []);

  const removeCandidate = useCallback((id: string) => {
    setCandidates((prev) => prev.filter((candidate) => candidate.id !== id));
  }, []);

  const refreshCandidateFromProvider = useCallback(
    async (id: string) => {
      const updated = await provider.getCandidate(id);
      if (updated) {
        applyCandidate(updated);
      } else {
        removeCandidate(id);
      }
      return updated;
    },
    [provider, applyCandidate, removeCandidate],
  );

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
      applyCandidate(updated);
      setLastAction(`Approved ${updated.title}.`);
      return updated;
    },
    [provider, applyCandidate],
  );

  const reject = useCallback(
    async (id: string, reason?: string) => {
      await provider.reject(id, reason);
      const updated = await refreshCandidateFromProvider(id);
      const note = reason ? ` (reason: ${reason})` : "";
      setLastAction(`Rejected fact "${updated?.title ?? id}"${note}.`);
    },
    [provider, refreshCandidateFromProvider],
  );

  const resolveContradiction = useCallback(
    async (
      contradictionId: string,
      resolution: "keep_primary" | "keep_rival",
      keepId: string,
      rivalTitle: string,
    ) => {
      const updated = await provider.resolveContradiction(
        contradictionId,
        resolution,
        keepId,
      );
      applyCandidate(updated);
      const affectedIds = Array.from(new Set(contradictionId.split("__")));
      await Promise.all(affectedIds.map((id) => refreshCandidateFromProvider(id)));
      setLastAction(`Resolved contradiction — kept ${keepId} over ${rivalTitle}.`);
      return updated;
    },
    [provider, applyCandidate, refreshCandidateFromProvider],
  );

  const resolveContradictionCustom = useCallback(
    async (contradictionId: string, customText: string) => {
      if (!provider.resolveContradictionCustom) {
        throw new Error("Custom resolution isn't available for this data source.");
      }
      const created = await provider.resolveContradictionCustom(contradictionId, customText);
      applyCandidate(created);
      // Both original sides are rejected server-side — refresh them so the UI drops
      // them from the active queue.
      const affectedIds = Array.from(new Set(contradictionId.split("__")));
      await Promise.all(affectedIds.map((id) => refreshCandidateFromProvider(id)));
      setLastAction(`Resolved contradiction with a custom answer: "${created.title}".`);
      return created;
    },
    [provider, applyCandidate, refreshCandidateFromProvider],
  );

  const createCandidate = useCallback(
    async (input: CandidateWriteInput) => {
      const created = await provider.createCandidate(input);
      applyCandidate(created);
      setLastAction(`Added eval "${created.title}".`);
      return created;
    },
    [provider, applyCandidate],
  );

  const updateCandidate = useCallback(
    async (id: string, input: CandidateWriteInput) => {
      const updated = await provider.updateCandidate(id, input);
      applyCandidate(updated);
      setLastAction(`Updated eval "${updated.title}".`);
      return updated;
    },
    [provider, applyCandidate],
  );

  const deleteCandidate = useCallback(
    async (id: string) => {
      const existing = candidates.find((c) => c.id === id);
      await provider.deleteCandidate(id);
      removeCandidate(id);
      setLastAction(`Deleted eval "${existing?.title ?? id}".`);
    },
    [provider, candidates, removeCandidate],
  );

  const refreshCandidate = useCallback(
    async (id: string) => {
      const updated = await refreshCandidateFromProvider(id);
      if (updated) {
        setLastAction(`Refreshed eval "${updated.title}".`);
      } else {
        setLastAction(`Removed eval ${id}; it no longer exists in this data source.`);
      }
      return updated;
    },
    [refreshCandidateFromProvider],
  );

  return {
    provider,
    candidates,
    loading,
    error,
    lastAction,
    clearLastAction: () => setLastAction(null),
    refresh,
    refreshCandidate,
    promote,
    reject,
    resolveContradiction,
    resolveContradictionCustom,
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
    filtered = filtered.filter((c) => c.state === stateFilter);
  }
  return filtered;
}
