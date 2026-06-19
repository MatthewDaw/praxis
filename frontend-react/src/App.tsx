import { useEffect, useMemo, useState } from "react";
import { ApiConflictError } from "./api/apiClient";
import { CandidateCards } from "./components/CandidateCards";
import { CandidateDetail } from "./components/CandidateDetail";
import { CandidateTable } from "./components/CandidateTable";
import { EvalMetricsEmbed } from "./components/EvalMetricsEmbed";
import { AppShell } from "./components/layout/AppShell";
import { ContentSplit } from "./components/layout/ContentSplit";
import { DashboardHeader } from "./components/layout/DashboardHeader";
import { FilterBar } from "./components/layout/FilterBar";
import { LoadingSkeleton } from "./components/ui/LoadingSkeleton";
import { filterCandidates, useCandidates } from "./hooks/useCandidates";
import "./index.css";

type ViewTab = "table" | "cards";

export default function App() {
  const {
    provider,
    candidates,
    loading,
    error,
    lastAction,
    clearLastAction,
    refresh,
    promote,
    reject,
    resolveContradiction,
  } = useCandidates();

  const [searchQuery, setSearchQuery] = useState("");
  const [stateFilter, setStateFilter] = useState("All");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [viewTab, setViewTab] = useState<ViewTab>("table");
  const [actionError, setActionError] = useState<string | null>(null);
  const [deferMessage, setDeferMessage] = useState<string | null>(null);

  const filtered = useMemo(
    () => filterCandidates(candidates, searchQuery, stateFilter),
    [candidates, searchQuery, stateFilter],
  );

  useEffect(() => {
    if (filtered.length === 0) {
      setSelectedId(null);
      return;
    }
    if (!selectedId || !filtered.some((c) => c.id === selectedId)) {
      setSelectedId(filtered[0].id);
    }
  }, [filtered, selectedId]);

  useEffect(() => {
    if (lastAction) {
      const timer = window.setTimeout(() => clearLastAction(), 6000);
      return () => window.clearTimeout(timer);
    }
  }, [lastAction, clearLastAction]);

  const apiUrl = import.meta.env.VITE_PRAXIS_API_BASE_URL?.trim();

  async function handlePromote(id: string) {
    setActionError(null);
    try {
      await promote(id);
    } catch (err) {
      if (err instanceof ApiConflictError) {
        setActionError("Conflict (409) — refresh and retry.");
        return;
      }
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleReject(id: string, reason?: string) {
    setActionError(null);
    try {
      await reject(id, reason);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  function handleDefer(primaryTitle: string, rivalTitle: string) {
    setDeferMessage(`Deferred contradiction between ${primaryTitle} and ${rivalTitle}.`);
    window.setTimeout(() => setDeferMessage(null), 5000);
  }

  async function handleResolve(
    contradictionId: string,
    resolution: "keep_primary" | "keep_rival",
    keepId: string,
    rivalTitle: string,
  ) {
    setActionError(null);
    try {
      await resolveContradiction(contradictionId, resolution, keepId, rivalTitle);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  const listView =
    viewTab === "table" ? (
      <CandidateTable
        candidates={filtered}
        selectedId={selectedId}
        onSelect={setSelectedId}
        onPromote={handlePromote}
        onReject={handleReject}
      />
    ) : (
      <CandidateCards
        candidates={filtered}
        selectedId={selectedId}
        onSelect={setSelectedId}
        onPromote={handlePromote}
        onReject={handleReject}
      />
    );

  return (
    <AppShell>
      <DashboardHeader
        apiUrl={apiUrl || undefined}
        onRefresh={() => void refresh()}
      />

      {lastAction ? <div className="success-banner">{lastAction}</div> : null}
      {deferMessage ? <div className="info-banner">{deferMessage}</div> : null}
      {actionError ? <div className="error-banner">{actionError}</div> : null}
      {error ? (
        <div className="error-banner">
          Backend unavailable — could not load candidates. ({error}) Unset{" "}
          <code>VITE_PRAXIS_API_BASE_URL</code> to use mock fixtures locally.
        </div>
      ) : null}

      <FilterBar
        searchQuery={searchQuery}
        stateFilter={stateFilter}
        viewTab={viewTab}
        candidateCount={filtered.length}
        onSearchChange={setSearchQuery}
        onStateFilterChange={setStateFilter}
        onViewTabChange={setViewTab}
      />

      {loading ? (
        <LoadingSkeleton />
      ) : (
        <ContentSplit
          list={listView}
          detail={
            <CandidateDetail
              candidates={filtered}
              selectedId={selectedId}
              onSelect={setSelectedId}
              onResolve={handleResolve}
              onDefer={handleDefer}
            />
          }
        />
      )}

      <EvalMetricsEmbed provider={provider} />

      <footer className="page-footer">
        React Knowledge Graph Dashboard · Integrates with Matthew&apos;s API via{" "}
        <code>VITE_PRAXIS_API_BASE_URL</code> · Does not import pipeline code directly ·
        Streamlit reference client in <code>frontend/</code>
      </footer>
    </AppShell>
  );
}
