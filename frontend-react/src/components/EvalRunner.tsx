import { useCallback, useEffect, useMemo, useState } from "react";
import {
  type ApiDataProviderAuth,
  type EvalCaseResult,
  type EvalRunResponse,
  type EvalScopesResponse,
  listEvalScopes,
  runEvalScope,
} from "../api/apiClient";

interface EvalRunnerProps {
  apiBaseUrl: string;
  auth?: string | ApiDataProviderAuth;
}

// Friendly labels for the override fields the API exposes.
const FIELD_LABELS: Record<string, string> = {
  substrate: "Graph substrate",
  embedder: "Embedder",
  reader: "Reader",
  ingest_model: "Ingest model",
  model: "Runner model",
  reader_top_k: "Reader top-k",
  reader_min_score: "Reader min score",
};
const NUMERIC_FIELDS = new Set(["reader_top_k", "reader_min_score"]);
const FREE_TEXT_PLACEHOLDER: Record<string, string> = {
  ingest_model: "e.g. openai/gpt-4o-mini",
  model: "e.g. sonnet / openai/gpt-4o-mini",
  reader_top_k: "e.g. 8",
  reader_min_score: "e.g. 0.2",
};

function statusColor(status: string): string {
  if (status === "PASS" || status === "XPASS") return "var(--success)";
  if (status === "SKIPPED") return "var(--action-edit-dark)";
  if (status === "XFAIL") return "var(--secondary)";
  return "var(--danger)";
}

export function EvalRunner({ apiBaseUrl, auth }: EvalRunnerProps) {
  const [meta, setMeta] = useState<EvalScopesResponse | null>(null);
  const [scope, setScope] = useState(".");
  const [backend, setBackend] = useState("openrouter");
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [showOverrides, setShowOverrides] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [run, setRun] = useState<EvalRunResponse | null>(null);

  const loadMeta = useCallback(async () => {
    setError(null);
    try {
      const data = await listEvalScopes(apiBaseUrl, auth);
      setMeta(data);
      setBackend((prev) => (data.backends.includes(prev) ? prev : data.backends[0] ?? "openrouter"));
      setScope((prev) =>
        data.scopes.some((s) => s.scope === prev) ? prev : data.scopes[0]?.scope ?? ".",
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [apiBaseUrl, auth]);

  useEffect(() => {
    void loadMeta();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBaseUrl]);

  const overrideFields = useMemo(
    () => (meta ? Object.entries(meta.overrideFields) : []),
    [meta],
  );
  const activeOverrides = useMemo(
    () => Object.fromEntries(Object.entries(overrides).filter(([, v]) => v !== "" && v != null)),
    [overrides],
  );

  async function handleRun() {
    setRunning(true);
    setError(null);
    setRun(null);
    try {
      setRun(await runEvalScope(apiBaseUrl, scope, backend, activeOverrides, auth));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  }

  const summary = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const r of run?.results ?? []) counts[r.status] = (counts[r.status] ?? 0) + 1;
    return counts;
  }, [run]);

  const overrideCount = Object.keys(activeOverrides).length;

  return (
    <section className="eval-runner">
      <header className="eval-runner__head">
        <h3 className="eval-runner__title">Run evals by scope</h3>
        <span className="eval-runner__hint">
          Runs every case in the folder (seed → agent → grade). Seeds are cached across cases.
        </span>
      </header>

      <div className="eval-runner__row">
        <label className="eval-runner__field">
          <span>Scope (folder)</span>
          <select value={scope} onChange={(e) => setScope(e.target.value)}>
            {meta?.scopes.map((s) => (
              <option key={s.scope} value={s.scope}>
                {s.scope === "." ? "all cases" : s.scope} ({s.caseCount})
              </option>
            ))}
          </select>
        </label>

        <label className="eval-runner__field">
          <span>Backend</span>
          <select value={backend} onChange={(e) => setBackend(e.target.value)}>
            {meta?.backends.map((b) => (
              <option key={b} value={b}>
                {b}
              </option>
            ))}
          </select>
        </label>

        <button
          type="button"
          className="eval-runner__toggle"
          onClick={() => setShowOverrides((v) => !v)}
          aria-expanded={showOverrides}
        >
          Config overrides{overrideCount ? ` (${overrideCount})` : ""} {showOverrides ? "▾" : "▸"}
        </button>

        <div className="eval-runner__actions">
          <button type="button" className="btn primary" onClick={() => void handleRun()} disabled={running}>
            {running ? "Running…" : "Run evals"}
          </button>
        </div>
      </div>

      {showOverrides ? (
        <div className="eval-runner__overrides">
          <p className="eval-runner__overrides-hint">
            Blank = use each case&apos;s default. Set a value to override it for this run.
          </p>
          <div className="eval-runner__grid">
            {overrideFields.map(([field, allowed]) => (
              <label key={field} className="eval-runner__field">
                <span>{FIELD_LABELS[field] ?? field}</span>
                {allowed ? (
                  <select
                    value={overrides[field] ?? ""}
                    onChange={(e) => setOverrides((o) => ({ ...o, [field]: e.target.value }))}
                  >
                    <option value="">default</option>
                    {allowed.map((opt) => (
                      <option key={opt} value={opt}>
                        {opt}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    type={NUMERIC_FIELDS.has(field) ? "number" : "text"}
                    step="any"
                    value={overrides[field] ?? ""}
                    placeholder={FREE_TEXT_PLACEHOLDER[field] ?? "default"}
                    onChange={(e) => setOverrides((o) => ({ ...o, [field]: e.target.value }))}
                  />
                )}
              </label>
            ))}
          </div>
        </div>
      ) : null}

      {error ? <p className="eval-runner__error">{error}</p> : null}

      {run ? (
        <div className="eval-runner__results">
          <div className="eval-runner__summary">
            <strong>{run.casesRun}</strong> case{run.casesRun === 1 ? "" : "s"} · {run.backend}
            {Object.entries(summary).map(([status, n]) => (
              <span key={status} className="eval-runner__chip" style={{ color: statusColor(status) }}>
                {status} {n}
              </span>
            ))}
          </div>
          <ul className="eval-runner__list">
            {run.results.map((r) => (
              <EvalCaseRow key={r.caseId} result={r} />
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

function EvalCaseRow({ result }: { result: EvalCaseResult }) {
  const [open, setOpen] = useState(false);
  const hasDetail =
    Boolean(result.output) ||
    Boolean(result.injectedKnowledge) ||
    Boolean(result.checks?.length) ||
    Boolean(result.error) ||
    Boolean(result.skipReasons?.length);

  return (
    <li className="eval-runner__case">
      <button
        type="button"
        className="eval-runner__case-head"
        onClick={() => hasDetail && setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="eval-runner__badge" style={{ background: statusColor(result.status) }}>
          {result.status}
        </span>
        <span className="eval-runner__case-id">{result.caseId}</span>
        {result.rubricScore != null ? (
          <span className="eval-runner__score">{(result.rubricScore * 100).toFixed(0)}%</span>
        ) : null}
        {hasDetail ? <span className="eval-runner__caret">{open ? "▾" : "▸"}</span> : null}
      </button>

      {open ? (
        <div className="eval-runner__case-body">
          {result.error ? <p className="eval-runner__error">{result.error}</p> : null}
          {result.skipReasons?.length ? (
            <p className="eval-runner__skip">Skipped: {result.skipReasons.join(", ")}</p>
          ) : null}
          {result.checks?.length ? (
            <ul className="eval-runner__checks">
              {result.checks.map((c) => (
                <li key={c.name}>
                  <span style={{ color: c.passed ? "var(--success)" : "var(--danger)" }}>
                    {c.passed ? "✓" : "✗"}
                  </span>{" "}
                  {c.name}
                  {c.evidence ? <span className="eval-runner__evidence"> — {c.evidence}</span> : null}
                </li>
              ))}
            </ul>
          ) : null}
          {result.output ? (
            <details>
              <summary>Agent output</summary>
              <pre className="eval-runner__pre">{result.output}</pre>
            </details>
          ) : null}
          {result.injectedKnowledge ? (
            <details>
              <summary>Injected knowledge (graph read)</summary>
              <pre className="eval-runner__pre">{result.injectedKnowledge}</pre>
            </details>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
