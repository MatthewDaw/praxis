import { useEffect, useMemo, useState } from "react";
import { ApiConflictError } from "./api/apiClient";
import { CandidateCards } from "./components/CandidateCards";
import { CandidateDetail } from "./components/CandidateDetail";
import { CandidateTable } from "./components/CandidateTable";
import { EvalMetricsEmbed } from "./components/EvalMetricsEmbed";
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

  async function handleReject(id: string) {
    setActionError(null);
    try {
      await reject(id);
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
      await resolveContradiction(contradictionId, resolution, keepId, rivalTitle);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">PRAXIS</span>
          <span className="brand-sub">Knowledge Graph Dashboard</span>
        </div>
        <button type="button" className="btn primary full" onClick={() => void refresh()}>
          Refresh data
        </button>
        <p className="sidebar-note">
          Contract:{" "}
          <a
            href="../docs/integration/candidate-api-v1.md"
            target="_blank"
            rel="noreferrer"
          >
            candidate-api-v1
          </a>
        </p>
        <p className="sidebar-note muted">
          Matthew implements the server; this React client targets the same endpoints as
          the Streamlit dashboard in <code>frontend/</code>.
        </p>
      </aside>

      <main className="main">
        <header className="page-header">
          <div>
            <h1>Candidate Review Gate</h1>
            <p>
              Review and promote AI-learned knowledge candidates from agent sessions.
            </p>
          </div>
          <p className="mode-banner">
            {apiUrl ? (
              <>
                Live API mode — <code>{apiUrl}</code>
              </>
            ) : (
              <>
                Mock mode — local fixtures only. Matthew&apos;s pipeline and Dominic&apos;s
                eval are not required to run this UI.
              </>
            )}
          </p>
        </header>

        {lastAction ? <div className="success-banner">{lastAction}</div> : null}
        {actionError ? <div className="error-banner">{actionError}</div> : null}
        {error ? (
          <div className="error-banner">
            Backend unavailable — could not load candidates. ({error}) Unset{" "}
            <code>VITE_PRAXIS_API_BASE_URL</code> to use mock fixtures locally.
          </div>
        ) : null}

        <section className="filters">
          <label>
            Search
            <input
              type="search"
              placeholder="Search by title or content..."
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
            />
          </label>
          <label>
            Filter by state
            <select
              value={stateFilter}
              onChange={(event) => setStateFilter(event.target.value)}
            >
              <option>All</option>
              <option>proposed</option>
              <option>suggested</option>
              <option>active</option>
              <option>decayed</option>
            </select>
          </label>
        </section>

        {loading ? <p className="muted">Loading candidates…</p> : null}

        <div className="tabs">
          <button
            type="button"
            className={viewTab === "table" ? "tab active" : "tab"}
            onClick={() => setViewTab("table")}
          >
            Table view
          </button>
          <button
            type="button"
            className={viewTab === "cards" ? "tab active" : "tab"}
            onClick={() => setViewTab("cards")}
          >
            Card view
          </button>
        </div>

        {viewTab === "table" ? (
          <CandidateTable
            candidates={filtered}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onPromote={handlePromote}
            onReject={handleReject}
          />
        ) : (
          <CandidateCards candidates={filtered} onSelect={setSelectedId} />
        )}

        <CandidateDetail
          candidates={filtered}
          selectedId={selectedId}
          onSelect={setSelectedId}
          onResolve={handleResolve}
        />

        <EvalMetricsEmbed provider={provider} />

        <footer className="page-footer">
          React Knowledge Graph Dashboard · Integrates with Matthew&apos;s API via{" "}
          <code>VITE_PRAXIS_API_BASE_URL</code> · Does not import pipeline code directly
        </footer>
      </main>
    </div>
  );
}
