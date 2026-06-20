import { useEffect, useMemo, useState } from "react";
import { ApiConflictError } from "./api/apiClient";
import { buildLocalLogSession } from "./api/localLogsProvider";
import { CandidateCards } from "./components/CandidateCards";
import { CandidateDetail } from "./components/CandidateDetail";
import { CandidateTable } from "./components/CandidateTable";
import {
  ContradictionsReview,
  uniqueContradictionPairs,
} from "./components/ContradictionsReview";
import { GraphExplorer } from "./components/graph/GraphExplorer";
import { EvalMetricsEmbed } from "./components/EvalMetricsEmbed";
import { McpSetupGuide } from "./components/McpSetupGuide";
import { AppShell } from "./components/layout/AppShell";
import { ContentSplit } from "./components/layout/ContentSplit";
import { DashboardHeader } from "./components/layout/DashboardHeader";
import { FilterBar } from "./components/layout/FilterBar";
import { CandidateEditorModal } from "./components/ui/CandidateEditorModal";
import { TranscriptPanel } from "./components/transcript/TranscriptPanel";
import { useApiHealth } from "./hooks/useApiHealth";
import { useDataSource } from "./hooks/useDataSource";
import { useGraph } from "./hooks/useGraph";
import { filterCandidates, useCandidates } from "./hooks/useCandidates";
import { PRESET_IDS } from "./config/dataSource";
import { LoadingSkeleton } from "./components/ui/LoadingSkeleton";
import { useOrg } from "./auth/OrgGate";
import type { Candidate, CandidateWriteInput } from "./types/candidate";
import type { LocalLogFileInput } from "./types/transcript";
import type { ViewTab } from "./types/view";
import "./index.css";

export default function App() {
  const [localSession, setLocalSession] = useState<ReturnType<typeof buildLocalLogSession> | null>(
    null,
  );
  const [localRawFiles, setLocalRawFiles] = useState<LocalLogFileInput[]>([]);
  const { getToken, orgId, signOut, switchOrg } = useOrg();
  const auth = useMemo(() => ({ getToken, orgId }), [getToken, orgId]);
  const { config, mode, label, detail, ingestApiBaseUrl, applyConfig } =
    useDataSource(localSession, auth);
  const [healthRefreshKey, setHealthRefreshKey] = useState(0);
  const [graphRefreshKey, setGraphRefreshKey] = useState(0);
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
    createCandidate,
    updateCandidate,
    deleteCandidate,
  } = useCandidates({ config, localSession, auth });

  const { graph, loading: graphLoading, error: graphError } = useGraph(
    provider,
    candidates,
    graphRefreshKey,
  );

  const [searchQuery, setSearchQuery] = useState("");
  const [stateFilter, setStateFilter] = useState("All");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [viewTab, setViewTab] = useState<ViewTab>("table");
  const [actionError, setActionError] = useState<string | null>(null);
  const [deferMessage, setDeferMessage] = useState<string | null>(null);
  const [infoMessage, setInfoMessage] = useState<string | null>(null);
  const [editorState, setEditorState] = useState<
    { mode: "add" } | { mode: "edit"; candidate: Candidate } | null
  >(null);
  const [editorPending, setEditorPending] = useState(false);

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
    if (infoMessage) {
      const timer = window.setTimeout(() => setInfoMessage(null), 8000);
      return () => window.clearTimeout(timer);
    }
  }, [infoMessage]);

  useEffect(() => {
    setSelectedId(null);
    setActionError(null);
  }, [config]);

  function bumpGraphRefresh() {
    setGraphRefreshKey((value) => value + 1);
  }

  function handleDataSourceLoad(presetId: string, customApiBaseUrl?: string) {
    setActionError(null);
    if (presetId !== PRESET_IDS.localLogs) {
      setLocalSession(null);
      setLocalRawFiles([]);
    }
    applyConfig(presetId, customApiBaseUrl);
    setHealthRefreshKey((value) => value + 1);
    bumpGraphRefresh();
    refetchHealth();
  }

  function handleLoadLocalLogs(files: LocalLogFileInput[]) {
    setActionError(null);
    applyConfig(PRESET_IDS.localLogs);
    const session = buildLocalLogSession(files);
    setLocalSession(session);
    setLocalRawFiles(files);
    setHealthRefreshKey((value) => value + 1);
    bumpGraphRefresh();
  }

  function handleClearLocalLogs() {
    setLocalSession(null);
    setLocalRawFiles([]);
    setActionError(null);
    bumpGraphRefresh();
  }

  function handleRefresh() {
    void refresh();
    setHealthRefreshKey((value) => value + 1);
    bumpGraphRefresh();
    refetchHealth();
  }

  async function handlePromote(id: string) {
    setActionError(null);
    try {
      await promote(id);
      bumpGraphRefresh();
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
      bumpGraphRefresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  function handleEditCandidate(candidate: Candidate) {
    setEditorState({ mode: "edit", candidate });
  }

  function handleAddEval() {
    setEditorState({ mode: "add" });
  }

  async function handleSaveCandidate(input: CandidateWriteInput) {
    setActionError(null);
    setEditorPending(true);
    try {
      if (editorState?.mode === "add") {
        const created = await createCandidate(input);
        setSelectedId(created.id);
        bumpGraphRefresh();
      } else if (editorState?.mode === "edit") {
        await updateCandidate(editorState.candidate.id, input);
        bumpGraphRefresh();
      }
      setEditorState(null);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
      throw err;
    } finally {
      setEditorPending(false);
    }
  }

  async function handleDelete(id: string) {
    setActionError(null);
    try {
      await deleteCandidate(id);
      bumpGraphRefresh();
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
      bumpGraphRefresh();
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
        onEdit={handleEditCandidate}
        onDelete={handleDelete}
      />
    ) : (
      <CandidateCards
        candidates={filtered}
        selectedId={selectedId}
        onSelect={setSelectedId}
        onPromote={handlePromote}
        onReject={handleReject}
        onEdit={handleEditCandidate}
        onDelete={handleDelete}
      />
    );

  const graphViewLoading = loading || graphLoading;
  const footerModeLabel =
    mode === "live"
      ? "Live API"
      : mode === "local-logs"
        ? "Local Claude logs"
        : "Mock fixtures (evals)";

  return (
    <AppShell>
      <DashboardHeader
        mode={mode}
        label={label}
        detail={detail}
        storeType={storeType}
        config={config}
        localSession={localSession}
        onDataSourceLoad={handleDataSourceLoad}
        onLoadLocalLogs={handleLoadLocalLogs}
        onClearLocalLogs={handleClearLocalLogs}
        onRefresh={handleRefresh}
      />

      {mode === "local-logs" ? (
        <div className="warning-banner">
          Heuristic preview — not Matthew&apos;s distillation pipeline. Upload .jsonl
          session files to explore transcripts and proposed candidates in the browser.
        </div>
      ) : null}

      {lastAction ? <div className="success-banner">{lastAction}</div> : null}
      {deferMessage ? <div className="info-banner">{deferMessage}</div> : null}
      {infoMessage ? <div className="info-banner">{infoMessage}</div> : null}
      {actionError ? <div className="error-banner">{actionError}</div> : null}
      {error ? (
        <div className="error-banner">
          Backend unavailable — could not load candidates. ({error}) Use the data
          source control above to switch to <strong>Mock fixtures (evals)</strong> or verify
          the live API URL and CORS settings.
        </div>
      ) : null}
      {graphError && viewTab === "graph" ? (
        <div className="error-banner">
          Graph snapshot unavailable ({graphError}) — showing derived fallback where possible.
        </div>
      ) : null}

      {mode === "local-logs" && localSession && localSession.lines.length > 0 ? (
        <TranscriptPanel
          session={localSession}
          candidates={candidates}
          apiBaseUrl={ingestApiBaseUrl}
          apiToken={config.apiToken}
          rawFiles={localRawFiles}
          onSelectCandidate={setSelectedId}
          onIngestSuccess={(message) => setInfoMessage(message)}
          onIngestError={(message) => setActionError(message)}
        />
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
        onAddEval={handleAddEval}
      />

      {viewTab === "setup" ? (
        <McpSetupGuide />
      ) : graphViewLoading ? (
        <LoadingSkeleton />
      ) : viewTab === "contradictions" ? (
        <ContradictionsReview
          candidates={candidates}
          onResolve={handleResolve}
          onDefer={handleDefer}
        />
      ) : viewTab === "graph" && graph ? (
        <GraphExplorer
          graph={graph}
          candidates={candidates}
          filteredCandidates={filtered}
          selectedId={selectedId}
          onSelectNode={setSelectedId}
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

      {viewTab !== "setup" ? <EvalMetricsEmbed provider={provider} /> : null}

      <CandidateEditorModal
        mode={editorState?.mode ?? "add"}
        candidate={editorState?.mode === "edit" ? editorState.candidate : undefined}
        open={editorState != null}
        pending={editorPending}
        onClose={() => setEditorState(null)}
        onSave={handleSaveCandidate}
      />

      <footer className="page-footer">
        React Knowledge Graph Dashboard · Data source: {footerModeLabel} · Org:{" "}
        <code>{orgId}</code> · candidate-api-v1 contract ·{" "}
        <button type="button" className="link-button" onClick={switchOrg}>
          Switch workspace
        </button>{" "}
        <button type="button" className="link-button" onClick={() => void signOut()}>
          Sign out
        </button>
      </footer>
    </AppShell>
  );
}
