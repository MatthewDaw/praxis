import { useState } from "react";
import type { ApiDataProviderAuth } from "../api/apiClient";
import {
  formatUtility,
  getContext,
  getFactDependents,
  getStaleDerivations,
  recordFactOutcome,
  utilityTone,
  type ContextHit,
  type FactDependent,
  type FactTrust,
} from "../api/contextClient";
import { MetadataGrid } from "./MetadataGrid";

interface ContextExplorerProps {
  apiBaseUrl: string;
  auth: ApiDataProviderAuth;
}

/** datetime-local value (local time, no zone) → ISO-8601 for the `as_of` param. */
function localToIso(value: string): string | undefined {
  if (!value.trim()) return undefined;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

function UtilityBadge({ trust }: { trust: FactTrust }) {
  const tone = utilityTone(trust.utility, trust.successCount, trust.failureCount);
  return (
    <span className={`utility-badge utility-badge--${tone}`} title="Laplace-smoothed H1 utility">
      {formatUtility(trust.utility)}
      <span className="utility-badge__counts">
        {" "}
        ({trust.successCount}✓ / {trust.failureCount}✗)
      </span>
    </span>
  );
}

/**
 * Operator surface over the compounding loop the agent factory drives via MCP:
 * point-in-time / episodic-aware `/context` recall, H1 outcome recording, and H5
 * derivation traversal (downstream dependents + stale-source flags).
 */
export function ContextExplorer({ apiBaseUrl, auth }: ContextExplorerProps) {
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(8);
  const [asOf, setAsOf] = useState("");
  const [includeEpisodic, setIncludeEpisodic] = useState(false);

  const [hits, setHits] = useState<ContextHit[] | null>(null);
  const [staleIds, setStaleIds] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [busyOutcome, setBusyOutcome] = useState<string | null>(null);
  const [dependents, setDependents] = useState<Record<string, FactDependent[]>>({});
  const [dependentsBusy, setDependentsBusy] = useState<string | null>(null);

  async function runQuery() {
    setLoading(true);
    setError(null);
    try {
      const [results, stale] = await Promise.all([
        getContext(
          apiBaseUrl,
          { query, topK, asOf: localToIso(asOf), includeEpisodic },
          auth,
        ),
        getStaleDerivations(apiBaseUrl, auth).catch(() => []),
      ]);
      setHits(results);
      setStaleIds(new Set(stale.map((s) => s.id)));
      setDependents({});
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setHits(null);
    } finally {
      setLoading(false);
    }
  }

  async function handleOutcome(hit: ContextHit, success: boolean) {
    setBusyOutcome(hit.id);
    setError(null);
    try {
      const trust = await recordFactOutcome(apiBaseUrl, hit.id, success, auth);
      setHits((prev) =>
        prev ? prev.map((h) => (h.id === hit.id ? { ...h, trust } : h)) : prev,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyOutcome(null);
    }
  }

  async function toggleDependents(hit: ContextHit) {
    if (dependents[hit.id]) {
      setDependents((prev) => {
        const next = { ...prev };
        delete next[hit.id];
        return next;
      });
      return;
    }
    setDependentsBusy(hit.id);
    setError(null);
    try {
      const deps = await getFactDependents(apiBaseUrl, hit.id, auth);
      setDependents((prev) => ({ ...prev, [hit.id]: deps }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDependentsBusy(null);
    }
  }

  return (
    <section className="context-explorer" aria-label="Context recall">
      <form
        className="context-form"
        onSubmit={(event) => {
          event.preventDefault();
          void runQuery();
        }}
      >
        <label className="context-form__field context-form__field--grow">
          Query
          <input
            type="text"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Recall facts similar to…"
            aria-label="Context query"
          />
        </label>
        <label className="context-form__field">
          Top K
          <input
            type="number"
            min={1}
            max={50}
            value={topK}
            onChange={(event) => setTopK(Number(event.target.value) || 1)}
            aria-label="Top K"
          />
        </label>
        <label className="context-form__field">
          As of (pin snapshot)
          <input
            type="datetime-local"
            value={asOf}
            onChange={(event) => setAsOf(event.target.value)}
            aria-label="As of timestamp"
          />
        </label>
        <label className="context-form__check">
          <input
            type="checkbox"
            checked={includeEpisodic}
            onChange={(event) => setIncludeEpisodic(event.target.checked)}
          />
          Include episodic
        </label>
        <button type="submit" className="btn" disabled={loading}>
          {loading ? "Recalling…" : "Recall context"}
        </button>
      </form>

      {error ? <div className="error-banner">{error}</div> : null}

      {hits == null ? (
        <p className="muted">
          Recall the active facts most similar to a query. Pin <code>as_of</code> for a
          point-in-time snapshot, or include episodic (store-only) entries.
        </p>
      ) : hits.length === 0 ? (
        <p className="muted">No facts matched this query.</p>
      ) : (
        <ul className="context-hits">
          {hits.map((hit) => {
            const isStale = staleIds.has(hit.id);
            const deps = dependents[hit.id];
            return (
              <li key={hit.id} className="context-hit">
                <div className="context-hit__head">
                  <p className="context-hit__text">{hit.text}</p>
                  {isStale ? (
                    <span
                      className="stale-badge"
                      title="A derived_from source for this fact was invalidated"
                    >
                      Stale source
                    </span>
                  ) : null}
                </div>

                <MetadataGrid
                  items={[
                    { label: "Source", value: <code className="mono small">{hit.source || "—"}</code> },
                    { label: "Scope", value: hit.scope || "—" },
                    { label: "Category", value: hit.category || "—" },
                    {
                      label: "Utility",
                      value: hit.trust ? <UtilityBadge trust={hit.trust} /> : <span className="muted">—</span>,
                    },
                  ]}
                />

                {hit.meta ? (
                  <details className="detail-section">
                    <summary>Meta</summary>
                    <pre>{JSON.stringify(hit.meta, null, 2)}</pre>
                  </details>
                ) : null}

                <div className="context-hit__actions">
                  <button
                    type="button"
                    className="btn secondary"
                    disabled={busyOutcome === hit.id}
                    onClick={() => void handleOutcome(hit, true)}
                  >
                    Mark worked
                  </button>
                  <button
                    type="button"
                    className="btn secondary"
                    disabled={busyOutcome === hit.id}
                    onClick={() => void handleOutcome(hit, false)}
                  >
                    Mark failed
                  </button>
                  <button
                    type="button"
                    className="btn secondary"
                    disabled={dependentsBusy === hit.id}
                    onClick={() => void toggleDependents(hit)}
                  >
                    {deps ? "Hide dependents" : "Show dependents"}
                  </button>
                </div>

                {deps ? (
                  deps.length === 0 ? (
                    <p className="muted small">No downstream learnings derived from this fact.</p>
                  ) : (
                    <ul className="context-dependents">
                      {deps.map((dep) => (
                        <li key={dep.id} className="context-dependents__item">
                          <span>{dep.text || dep.id}</span>
                          {dep.stale ? <span className="stale-badge">Stale</span> : null}
                        </li>
                      ))}
                    </ul>
                  )
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
