import { useCallback, useEffect, useState } from "react";
import {
  type ApiDataProviderAuth,
  type EvalScope,
  EvalRegenerateUnavailableError,
  listEvalScopes,
  regenerateGraphFromScopes,
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

  const loadScopes = useCallback(async () => {
    try {
      const data = await listEvalScopes(apiBaseUrl, auth);
      setScopes(data.scopes);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [apiBaseUrl, auth]);

  useEffect(() => {
    void loadScopes();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBaseUrl]);

  async function handleLoad(distill: boolean) {
    if (!selected.length) return;
    setLoading(true);
    setError(null);
    setMessage(null);
    try {
      const result = await regenerateGraphFromScopes(apiBaseUrl, selected, distill, auth);
      setMessage(
        `${distill ? "Distilled" : "Loaded"} ${result.candidatesInserted} candidates from ${result.casesRun} cases.`,
      );
      onLoaded?.();
    } catch (err) {
      if (err instanceof EvalRegenerateUnavailableError) {
        setError(`Unavailable: ${err.message}`);
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
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
          <ScopePicker scopes={scopes} selected={selected} onChange={setSelected} />
          <div className="eval-runner__row">
            <div className="eval-runner__actions">
              <button
                type="button"
                className="btn primary"
                onClick={() => void handleLoad(false)}
                disabled={loading || !selected.length}
                title="Read the seed text straight into the graph — offline, instant"
              >
                {loading ? "Working…" : `Load seed data${selected.length ? ` (${selected.length})` : ""}`}
              </button>
              <button
                type="button"
                className="btn secondary"
                onClick={() => void handleLoad(true)}
                disabled={loading || !selected.length}
                title="Run the real distillation pipeline (LLM + embeddings) — slow, uses credits"
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
