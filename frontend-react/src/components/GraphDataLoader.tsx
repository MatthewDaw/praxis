import { useCallback, useEffect, useState } from "react";
import {
  type ApiDataProviderAuth,
  type EvalScope,
  EvalRegenerateUnavailableError,
  listCachedEvalCases,
  listEvalScopes,
  loadEvals,
  regenerateEvalCache,
} from "../api/apiClient";
import { ScopePicker } from "./ScopePicker";

interface GraphDataLoaderProps {
  apiBaseUrl: string;
  auth?: string | ApiDataProviderAuth;
  /** Called after a successful load so the dashboard can refresh candidates/graph. */
  onLoaded?: () => void;
}

/** Pick eval folders/cases and ingest their seed knowledge into the graph view. */
export function GraphDataLoader({ apiBaseUrl, auth, onLoaded }: GraphDataLoaderProps) {
  const [scopes, setScopes] = useState<EvalScope[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [cached, setCached] = useState<Set<string>>(new Set());

  const loadScopes = useCallback(async () => {
    try {
      const data = await listEvalScopes(apiBaseUrl, auth);
      setScopes(data.scopes);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [apiBaseUrl, auth]);

  const loadCached = useCallback(async () => {
    try {
      setCached(new Set(await listCachedEvalCases(apiBaseUrl, auth)));
    } catch {
      /* cached-status is best-effort; ignore failures */
    }
  }, [apiBaseUrl, auth]);

  useEffect(() => {
    void loadScopes();
    void loadCached();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBaseUrl]);

  function handleError(err: unknown) {
    if (err instanceof EvalRegenerateUnavailableError) {
      setError(`Unavailable: ${err.message}`);
    } else {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleLoadEvals(mode: "add" | "replace") {
    if (!selected.length) return;
    if (mode === "replace") {
      const ok = window.confirm(
        "Replace graph clears the entire live graph before inserting the selected evals. Continue?",
      );
      if (!ok) return;
    }
    setLoading(true);
    setError(null);
    setMessage(null);
    try {
      const result = await loadEvals(apiBaseUrl, { scopes: selected, mode, distill: false }, auth);
      setMessage(
        `${mode === "replace" ? "Replaced graph with" : "Added"} ${result.candidatesInserted} candidates into the graph.`,
      );
      onLoaded?.();
      void loadCached();
    } catch (err) {
      handleError(err);
    } finally {
      setLoading(false);
    }
  }

  async function handleRegenerateCache() {
    if (!selected.length) return;
    setLoading(true);
    setError(null);
    setMessage(null);
    try {
      const result = await regenerateEvalCache(
        apiBaseUrl,
        { scopes: selected, distill: true },
        auth,
      );
      setMessage(`Cached ${result.casesCached} eval cases (graph unchanged).`);
      // Cache-only: do NOT refresh the graph/candidates. Only refresh the dots.
      void loadCached();
    } catch (err) {
      handleError(err);
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="eval-runner">
      <header className="eval-runner__head">
        <button
          type="button"
          className="eval-runner__collapse"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          {open ? "▾" : "▸"} <span className="eval-runner__title">Load eval data into graph</span>
        </button>
        <span className="eval-runner__hint">
          Load the seed text from selected folders/cases (fast), or run the distillation pipeline.
        </span>
      </header>

      {open ? (
        <>
          <ScopePicker scopes={scopes} selected={selected} onChange={setSelected} cached={cached} />
          <div className="eval-runner__row">
            <div className="eval-runner__actions">
              <button
                type="button"
                className="btn primary"
                onClick={() => void handleLoadEvals("add")}
                disabled={loading || !selected.length}
                title="Add these evals to the graph (replaces only their own nodes)"
                aria-label="Add these evals to the graph (replaces only their own nodes)"
              >
                {loading ? "Working…" : `Add evals${selected.length ? ` (${selected.length})` : ""}`}
              </button>
              <button
                type="button"
                className="btn primary"
                onClick={() => void handleLoadEvals("replace")}
                disabled={loading || !selected.length}
                title="Clear the whole live graph, then insert these evals"
                aria-label="Clear the whole live graph, then insert these evals"
              >
                {loading ? "Working…" : `Replace graph${selected.length ? ` (${selected.length})` : ""}`}
              </button>
              <button
                type="button"
                className="btn secondary"
                onClick={() => void handleRegenerateCache()}
                disabled={loading || !selected.length}
                title="Run the real distillation pipeline (LLM + embeddings) into the cache only — does not change the graph"
              >
                {loading ? "Working…" : "Run pipeline (distill)"}
              </button>
            </div>
          </div>
          {error ? <p className="eval-runner__error">{error}</p> : null}
          {message ? <p className="eval-runner__loaded">{message}</p> : null}
        </>
      ) : null}
    </section>
  );
}
