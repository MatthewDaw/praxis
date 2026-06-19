import { useEffect, useMemo, useState } from "react";
import { ApiConflictError } from "./api/apiClient";
import { CandidateCards } from "./components/CandidateCards";
import { CandidateDetail } from "./components/CandidateDetail";
import { CandidateTable } from "./components/CandidateTable";
import {
  ContradictionsReview,
  uniqueContradictionPairs,
} from "./components/ContradictionsReview";
import { EvalMetricsEmbed } from "./components/EvalMetricsEmbed";
import { AppShell } from "./components/layout/AppShell";
import { ContentSplit } from "./components/layout/ContentSplit";
import { DashboardHeader } from "./components/layout/DashboardHeader";
import { FilterBar } from "./components/layout/FilterBar";
import { LoadingSkeleton } from "./components/ui/LoadingSkeleton";
import { useApiHealth } from "./hooks/useApiHealth";
import { useDataSource } from "./hooks/useDataSource";
import { filterCandidates, useCandidates } from "./hooks/useCandidates";
import "./index.css";

type ViewTab = "table" | "cards" | "contradictions";

export default function App() {
  const { config, mode, label, detail, applyConfig } = useDataSource();
  const [healthRefreshKey, setHealthRefreshKey] = useState(0);
  const { storeType, refetch: refetchHealth } = useApiHealth(config, healthRefreshKey);
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
  } = useCandidates(config);

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

  const contradictionCount = useMemo(
    () => uniqueContradictionPairs(candidates).length,
    [candidates],
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

  useEffect(() => {
    setSelectedId(null);
    setActionError(null);
  }, [config]);

  function handleDataSourceLoad(presetId: string, customApiBaseUrl?: string) {
    setActionError(null);
    applyConfig(presetId, customApiBaseUrl);
    setHealthRefreshKey((value) => value + 1);
    refetchHealth();
  }

  function handleRefresh() {
    void refresh();
    setHealthRefreshKey((value) => value + 1);
    refetchHealth();
  }

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
        mode={mode}
        label={label}
        detail={detail}
        storeType={storeType}
        config={config}
        onDataSourceLoad={handleDataSourceLoad}
        onRefresh={handleRefresh}
      />

      {lastAction ? <div className="success-banner">{lastAction}</div> : null}
      {deferMessage ? <div className="info-banner">{deferMessage}</div> : null}
      {actionError ? <div className="error-banner">{actionError}</div> : null}
      {error ? (
        <div className="error-banner">
          Backend unavailable — could not load candidates. ({error}) Use the data
          source control above to switch to <strong>Mock fixtures</strong> or verify
          the live API URL and CORS settings.
        </div>
      ) : null}

      <FilterBar
        searchQuery={searchQuery}
        stateFilter={stateFilter}
        viewTab={viewTab}
        candidateCount={filtered.length}
        contradictionCount={contradictionCount}
        onSearchChange={setSearchQuery}
        onStateFilterChange={setStateFilter}
        onViewTabChange={setViewTab}
      />

      {loading ? (
        <LoadingSkeleton />
      ) : viewTab === "contradictions" ? (
        <ContradictionsReview
          candidates={candidates}
          onResolve={handleResolve}
          onDefer={handleDefer}
        />
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
        React Knowledge Graph Dashboard · Data source: {mode === "live" ? "Live API" : "Mock fixtures"} ·
        candidate-api-v1 contract · Streamlit reference client in <code>frontend/</code>
      </footer>
    </AppShell>
  );
}
