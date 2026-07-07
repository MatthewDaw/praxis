import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiConflictError,
  GraphIngestUnavailableError,
  loadSnapshot,
  postInsight,
  saveSnapshot,
} from "./api/apiClient";
import { recordFactOutcome } from "./api/contextClient";
import { canDeleteCandidate } from "./api/candidateModel";
import { buildLocalLogSession } from "./api/localLogsProvider";
import { CandidateCards } from "./components/CandidateCards";
import { CandidateDetail } from "./components/CandidateDetail";
import { ContextExplorer } from "./components/ContextExplorer";
import { ApiKeysPanel } from "./components/ApiKeysPanel";
import { CandidateTable } from "./components/CandidateTable";
import {
  ContradictionsReview,
  contradictionClusters,
  type ContradictionCluster,
  type ContradictionPair,
} from "./components/ContradictionsReview";
import type { ContradictionClusterWire } from "./api/dataProvider";
import { GraphExplorer } from "./components/graph/GraphExplorer";
import { McpSetupGuide } from "./components/McpSetupGuide";
import { AppShell } from "./components/layout/AppShell";
import { ContentSplit } from "./components/layout/ContentSplit";
import { DashboardHeader } from "./components/layout/DashboardHeader";
import { FilterBar } from "./components/layout/FilterBar";
import { SectionTabs } from "./components/layout/SectionTabs";
import { CandidateEditorModal } from "./components/ui/CandidateEditorModal";
import { Modal } from "./components/ui/Modal";
import { MountSwitcher } from "./components/ui/MountSwitcher";
import { SnapshotSwitcher } from "./components/ui/SnapshotSwitcher";
import { TranscriptPanel } from "./components/transcript/TranscriptPanel";
import { useApiHealth } from "./hooks/useApiHealth";
import { useDataSource } from "./hooks/useDataSource";
import { useGraph } from "./hooks/useGraph";
import { filterCandidates, useCandidates } from "./hooks/useCandidates";
import { PRESET_IDS } from "./config/dataSource";
import { LoadingSkeleton } from "./components/ui/LoadingSkeleton";
import { useOrg } from "./auth/OrgGate";
import { useSpace } from "./auth/SpaceGate";
import type { Candidate, CandidateWriteInput } from "./types/candidate";
import type { LocalLogFileInput } from "./types/transcript";
import type { ViewTab } from "./types/view";
import "./index.css";

/**
 * Hydrate the backend's slot-aware clusters (ids only) into the shape the review
 * UI renders, by looking each member/pair id up in the loaded candidates. The
 * grouping is the backend's — this only attaches full candidate objects.
 */
function hydrateClusters(
  wire: ContradictionClusterWire[],
  candidates: Candidate[],
): ContradictionCluster[] {
  const byId = new Map(candidates.map((c) => [c.id, c]));
  const clusters: ContradictionCluster[] = [];
  for (const w of wire) {
    const members = w.members
      .map((m) => byId.get(m.id))
      .filter((c): c is Candidate => !!c);
    if (members.length < 2) continue;
    const pairs: ContradictionPair[] = [];
    for (const p of w.pairs) {
      const primary = byId.get(p.a.id);
      const rival = byId.get(p.b.id);
      if (primary && rival) pairs.push({ primary, rival });
    }
    clusters.push({
      id: w.id,
      slot: w.slot ? { subject: w.slot.subject, attribute: w.slot.attribute } : null,
      members,
      pairs,
    });
  }
  return clusters;
}

export default function App() {
  const [localSession, setLocalSession] = useState<ReturnType<typeof buildLocalLogSession> | null>(
    null,
  );
  const [localRawFiles, setLocalRawFiles] = useState<LocalLogFileInput[]>([]);
  const { getToken, orgId, orgName, userId, email, signOut, switchOrg } = useOrg();
  const { spaceId } = useSpace();
  const auth = useMemo(
    () => ({ getToken, orgId, spaceId }),
    [getToken, orgId, spaceId],
  );

  // --- Snapshot quick-switch state ---------------------------------------
  // The snapshot the live graph was last loaded from / saved to, tracked per
  // (org, space) so switching either never carries a stale name. `dirty` flips
  // on any graph edit since that snapshot was loaded and powers the "pending
  // save" light; it is session-only (a reload conservatively resets to clean).
  const snapshotStorageKey = `praxis-active-snapshot:${orgId}:${spaceId}`;
  const [activeSnapshot, setActiveSnapshot] = useState<string>(
    () => localStorage.getItem(`praxis-active-snapshot:${orgId}:${spaceId}`) ?? "",
  );
  const [snapshotDirty, setSnapshotDirty] = useState(false);

  useEffect(() => {
    setActiveSnapshot(localStorage.getItem(`praxis-active-snapshot:${orgId}:${spaceId}`) ?? "");
    setSnapshotDirty(false);
  }, [orgId, spaceId]);

  const markGraphDirty = useCallback(() => setSnapshotDirty(true), []);

  const handleSnapshotSynced = useCallback(
    (name: string) => {
      setActiveSnapshot(name);
      if (name) {
        localStorage.setItem(snapshotStorageKey, name);
      } else {
        localStorage.removeItem(snapshotStorageKey);
      }
      setSnapshotDirty(false);
    },
    [snapshotStorageKey],
  );
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
    refreshCandidate,
    promote,
    reject,
    resolveContradiction,
    resolveContradictionCustom,
    createCandidate,
    updateCandidate,
    deleteCandidate,
  } = useCandidates({ config, localSession, auth });

  const [searchQuery, setSearchQuery] = useState("");
  const [stateFilter, setStateFilter] = useState("All");

  // The graph mirrors the state filter: "All" → every lifecycle state, otherwise
  // just the selected state. Default ("active") stays one-to-one with retrieval.
  const graphState = stateFilter === "All" ? "all" : stateFilter;
  const { graph, loading: graphLoading, error: graphError } = useGraph(
    provider,
    candidates,
    graphRefreshKey,
    graphState,
  );

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [viewTab, setViewTab] = useState<ViewTab>("table");
  const [activePanel, setActivePanel] = useState<null | "apikeys">(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [reviewNotice, setReviewNotice] = useState<string | null>(null);
  const [infoMessage, setInfoMessage] = useState<string | null>(null);
  const [refreshingCandidateId, setRefreshingCandidateId] = useState<string | null>(
    null,
  );
  const [recordingOutcomeId, setRecordingOutcomeId] = useState<string | null>(null);
  const [editorState, setEditorState] = useState<
    { mode: "add" } | { mode: "edit"; candidate: Candidate } | null
  >(null);
  const [editorPending, setEditorPending] = useState(false);

  const filtered = useMemo(
    () => filterCandidates(candidates, searchQuery, stateFilter),
    [candidates, searchQuery, stateFilter],
  );

  // Slot-aware clusters come from the backend (GET /contradictions); offline
  // providers that can't compute them fall back to client-side clustering. Refetch
  // when candidates change so resolutions drop their clusters from the queue.
  const [wireClusters, setWireClusters] = useState<ContradictionClusterWire[] | null>(
    null,
  );
  useEffect(() => {
    if (!provider.getContradictions) {
      setWireClusters(null);
      return;
    }
    let active = true;
    provider
      .getContradictions()
      .then((w) => {
        if (active) setWireClusters(w);
      })
      .catch(() => {
        if (active) setWireClusters([]);
      });
    return () => {
      active = false;
    };
  }, [provider, candidates]);

  const contradictionClusterList = useMemo(
    () =>
      wireClusters
        ? hydrateClusters(wireClusters, candidates)
        : contradictionClusters(candidates),
    [wireClusters, candidates],
  );
  const contradictionCount = contradictionClusterList.length;

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

  // Reconcile a restored "active snapshot" label with reality. The active-snapshot
  // name is persisted in localStorage and restored on mount / space switch, but the
  // live working-memory graph it claims to be loaded from is server-side and may be
  // empty (a fresh session, a new browser, or a different space) — leaving the
  // dropdown advertising "prd-team-app (205 nodes)" while the candidate list reads
  // an empty live graph and shows "0 candidates". When a snapshot is marked active
  // and CLEAN but the live graph came back empty, load it once so those nodes
  // actually appear. Guards keep it safe: only into an empty graph (never clobbers
  // live edits), never while dirty (so an intentional Truncate, which marks the
  // graph dirty, is respected), and at most once per (org, space, snapshot).
  const reconciledSnapshotRef = useRef<string>("");
  useEffect(() => {
    if (mode !== "live" || !config.apiBaseUrl) return;
    if (!activeSnapshot || snapshotDirty || loading || error) return;
    if (candidates.length > 0) return;
    const key = `${orgId}:${spaceId}:${activeSnapshot}`;
    if (reconciledSnapshotRef.current === key) return;
    reconciledSnapshotRef.current = key;
    const apiBaseUrl = config.apiBaseUrl;
    void (async () => {
      try {
        await loadSnapshot(apiBaseUrl, activeSnapshot, "replace", auth);
        void refresh();
        setGraphRefreshKey((value) => value + 1);
      } catch {
        // Stale/missing snapshot or a transient error — leave the live graph as-is
        // (the user can pick another snapshot). The ref guard prevents a retry loop.
      }
    })();
  }, [
    mode,
    config.apiBaseUrl,
    activeSnapshot,
    snapshotDirty,
    loading,
    error,
    candidates.length,
    orgId,
    spaceId,
    auth,
    refresh,
  ]);

  useEffect(() => {
    setSelectedId(null);
    setActionError(null);
  }, [config]);

  function bumpGraphRefresh() {
    setGraphRefreshKey((value) => value + 1);
  }

  async function ingestActiveCandidate(candidate: Candidate) {
    if (candidate.state !== "active" || mode !== "live" || !config.apiBaseUrl) {
      return;
    }

    const insight = candidate.content.trim();
    if (!insight) {
      setInfoMessage(
        `"${candidate.title}" is active; graph ingest skipped because it has no content.`,
      );
      return;
    }

    try {
      const result = await postInsight(config.apiBaseUrl, insight, auth);
      const idSuffix = result.id ? ` (${result.id})` : "";
      setInfoMessage(`Graph ingest via /insights: ${result.summary}${idSuffix}.`);
      bumpGraphRefresh();
      markGraphDirty();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (err instanceof GraphIngestUnavailableError) {
        setInfoMessage(
          `"${candidate.title}" is active; graph ingest skipped: ${message}.`,
        );
        return;
      }
      setActionError(
        `"${candidate.title}" is active, but graph ingest failed: ${message}`,
      );
    }
  }

  function handleDataSourceLoad(presetId: string, customApiBaseUrl?: string) {
    setActionError(null);
    if (presetId !== PRESET_IDS.localLogs) {
      setLocalSession(null);
      setLocalRawFiles([]);
    }
    const next = applyConfig(presetId, customApiBaseUrl);
    // Switching to a different live server changes which backend owns auth and
    // org membership. OrgGate validates membership against the base URL it
    // resolved at mount, so a runtime swap would leave a stale org context
    // (e.g. "member of org 'praxis'" on localhost but not on RDS → 403). The new
    // config is already persisted, so reload to re-gate against the new server.
    if (next.apiBaseUrl !== config.apiBaseUrl) {
      window.location.reload();
      return;
    }
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

  async function handleRefreshCandidate(id: string) {
    setActionError(null);
    setRefreshingCandidateId(id);
    try {
      await refreshCandidate(id);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setRefreshingCandidateId(null);
    }
  }

  async function handleRecordOutcome(id: string, success: boolean) {
    if (mode !== "live" || !config.apiBaseUrl) {
      return;
    }
    setActionError(null);
    setRecordingOutcomeId(id);
    try {
      await recordFactOutcome(config.apiBaseUrl, id, success, auth);
      await refreshCandidate(id);
      markGraphDirty();
      setInfoMessage(
        `Recorded ${success ? "success" : "failure"} outcome for the fact.`,
      );
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setRecordingOutcomeId(null);
    }
  }

  function showReviewNotice() {
    setReviewNotice(
      "This retirement affects a fact with other contradictions — review it.",
    );
    window.setTimeout(() => setReviewNotice(null), 6000);
  }

  function noticeFromResult(result: Candidate) {
    const extra = result.extra ?? {};
    const rejected = extra.rejected;
    const rippleFromRejected =
      Array.isArray(rejected) &&
      rejected.some(
        (row) =>
          !!row &&
          typeof row === "object" &&
          (row as { hasOtherContradictions?: unknown }).hasOtherContradictions === true,
      );
    if (extra.hasOtherContradictions === true || rippleFromRejected) {
      showReviewNotice();
    }
  }

  async function handlePromote(id: string) {
    setActionError(null);
    try {
      const updated = await promote(id);
      markGraphDirty();
      noticeFromResult(updated);
      await ingestActiveCandidate(updated);
    } catch (err) {
      if (err instanceof ApiConflictError) {
        setActionError("Conflict (409) — refresh this candidate and retry.");
        return;
      }
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleReject(id: string, reason?: string) {
    setActionError(null);
    try {
      // provider.reject resolves to void (no Candidate body), so there is no
      // extra.hasOtherContradictions signal to surface on this path.
      await reject(id, reason);
      markGraphDirty();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  function handleEditCandidate(candidate: Candidate) {
    setEditorState({ mode: "edit", candidate });
  }

  async function handleClearGraph() {
    const ok = window.confirm(
      "Clear graph permanently removes every fact and edge in YOUR graph (this user only). This cannot be undone. Continue?",
    );
    if (!ok) return;
    setActionError(null);
    try {
      const { cleared } = await provider.clearGraph();
      setInfoMessage(`Cleared your graph — removed ${cleared} fact${cleared === 1 ? "" : "s"}.`);
      await refresh();
      setGraphRefreshKey((value) => value + 1);
      markGraphDirty();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  // Save the live graph into the currently-selected snapshot. With no snapshot
  // selected, prompt for a name and create a new one.
  async function handleSaveSnapshot() {
    if (!(mode === "live" && config.apiBaseUrl)) return;
    let name = activeSnapshot;
    if (!name) {
      const entered = window.prompt("Save the current graph as a snapshot named:")?.trim();
      if (!entered) return;
      name = entered;
    }
    setActionError(null);
    try {
      await saveSnapshot(config.apiBaseUrl, name, auth);
      handleSnapshotSynced(name);
      setInfoMessage(`Saved graph to snapshot "${name}".`);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleSaveCandidate(input: CandidateWriteInput) {
    setActionError(null);
    setEditorPending(true);
    try {
      if (editorState?.mode === "add") {
        const created = await createCandidate(input);
        setSelectedId(created.id);
      } else if (editorState?.mode === "edit") {
        const updated = await updateCandidate(editorState.candidate.id, input);
        await ingestActiveCandidate(updated);
      }
      markGraphDirty();
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
    const candidate = candidates.find((c) => c.id === id);
    if (candidate && !canDeleteCandidate(candidate)) {
      setActionError("Reject this fact before deleting it.");
      return;
    }
    try {
      await deleteCandidate(id);
      markGraphDirty();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleResolve(
    contradictionId: string,
    resolution: "keep_primary" | "keep_rival",
    keepId: string,
    rivalTitle: string,
  ) {
    setActionError(null);
    try {
      const updated = await resolveContradiction(
        contradictionId,
        resolution,
        keepId,
        rivalTitle,
      );
      markGraphDirty();
      if (updated) {
        noticeFromResult(updated);
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleResolveCustom(contradictionId: string, customText: string) {
    setActionError(null);
    try {
      await resolveContradictionCustom(contradictionId, customText);
      markGraphDirty();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  const knowledgeView =
    viewTab === "graph" ? (
      graph ? (
        <GraphExplorer
          graph={graph}
          candidates={candidates}
          selectedId={selectedId}
          onSelectNode={setSelectedId}
        />
      ) : (
        <p className="muted">No graph snapshot is available.</p>
      )
    ) : viewTab === "table" ? (
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

  const snapshotControl =
    mode === "live" && config.apiBaseUrl ? (
      <SnapshotSwitcher
        apiBaseUrl={config.apiBaseUrl}
        auth={auth}
        activeSnapshot={activeSnapshot}
        dirty={snapshotDirty}
        onSynced={handleSnapshotSynced}
        onGraphReplaced={handleRefresh}
      />
    ) : undefined;

  const mountControl =
    mode === "live" && config.apiBaseUrl ? (
      <MountSwitcher apiBaseUrl={config.apiBaseUrl} auth={auth} />
    ) : undefined;

  const headerTools = (
    <div className="header-tools">
      <button
        type="button"
        className="header-tools__btn"
        onClick={() => setActivePanel("apikeys")}
      >
        API keys
      </button>
    </div>
  );

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
        tools={mode === "live" && config.apiBaseUrl ? headerTools : undefined}
        snapshot={snapshotControl}
        tabs={
          <SectionTabs
            viewTab={viewTab}
            contradictionCount={contradictionCount}
            onViewTabChange={setViewTab}
          />
        }
      />

      {mountControl}

      {mode === "local-logs" ? (
        <div className="warning-banner">
          Heuristic preview — not Matthew&apos;s distillation pipeline. Upload .jsonl
          session files to explore transcripts and proposed candidates in the browser.
        </div>
      ) : null}

      {mode === "live" && config.apiBaseUrl && activePanel === "apikeys" ? (
        <Modal title="API keys" onClose={() => setActivePanel(null)}>
          <ApiKeysPanel apiBaseUrl={config.apiBaseUrl} auth={auth} embedded />
        </Modal>
      ) : null}

      {lastAction ? <div className="success-banner">{lastAction}</div> : null}
      {reviewNotice ? (
        <div className="info-banner" role="status">
          {reviewNotice}
        </div>
      ) : null}
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

      {viewTab !== "setup" && viewTab !== "contradictions" && viewTab !== "context" ? (
        <FilterBar
          searchQuery={searchQuery}
          stateFilter={stateFilter}
          viewTab={viewTab}
          candidateCount={filtered.length}
          onSearchChange={setSearchQuery}
          onStateFilterChange={setStateFilter}
          onViewTabChange={setViewTab}
          onClearGraph={handleClearGraph}
          onSaveSnapshot={
            mode === "live" && config.apiBaseUrl ? handleSaveSnapshot : undefined
          }
          saveTarget={activeSnapshot}
        />
      ) : null}

      {viewTab === "setup" ? (
        <McpSetupGuide email={email} />
      ) : viewTab === "context" ? (
        mode === "live" && config.apiBaseUrl ? (
          <ContextExplorer apiBaseUrl={config.apiBaseUrl} auth={auth} />
        ) : (
          <p className="muted">
            Context recall reads the live knowledge graph — switch the data source to a
            live API to query <code>/context</code>, record outcomes, and trace
            derivations.
          </p>
        )
      ) : graphViewLoading ? (
        <LoadingSkeleton />
      ) : viewTab === "contradictions" ? (
        <ContradictionsReview
          clusters={contradictionClusterList}
          onResolve={handleResolve}
          onResolveCustom={handleResolveCustom}
        />
      ) : (
        <ContentSplit
          list={knowledgeView}
          detail={
            <CandidateDetail
              candidates={filtered}
              selectedId={selectedId}
              onSelect={setSelectedId}
              onPromote={handlePromote}
              onReject={handleReject}
              onRefreshCandidate={handleRefreshCandidate}
              refreshingId={refreshingCandidateId}
              onResolve={handleResolve}
              onDelete={handleDelete}
              onRecordOutcome={
                mode === "live" && config.apiBaseUrl ? handleRecordOutcome : undefined
              }
              recordingOutcomeId={recordingOutcomeId}
              dataSourceMode={mode}
            />
          }
        />
      )}

      <CandidateEditorModal
        mode={editorState?.mode ?? "add"}
        candidate={editorState?.mode === "edit" ? editorState.candidate : undefined}
        open={editorState != null}
        pending={editorPending}
        onClose={() => setEditorState(null)}
        onSave={handleSaveCandidate}
      />

      <footer className="page-footer">
        <div>
          React Knowledge Graph Dashboard · Data source: {footerModeLabel} ·
          candidate-api-v1 contract
        </div>
        <div className="page-footer__account">
          User: <code>{userId || "—"}</code> · Org:{" "}
          <code>{orgName && orgName !== orgId ? `${orgName} (${orgId})` : orgId}</code>
        </div>
        <div className="page-footer__actions">
          <button type="button" className="link-button" onClick={switchOrg}>
            Switch workspace
          </button>{" "}
          <button type="button" className="link-button" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </footer>
    </AppShell>
  );
}
