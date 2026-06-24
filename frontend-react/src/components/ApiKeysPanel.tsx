import { useCallback, useEffect, useState } from "react";
import {
  type ApiDataProviderAuth,
  type ApiKey,
  type CreatedApiKey,
  createApiKey,
  listApiKeys,
  revokeApiKey,
} from "../api/apiClient";

interface ApiKeysPanelProps {
  apiBaseUrl: string;
  auth?: string | ApiDataProviderAuth;
}

function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

/**
 * In-page management of scoped API keys: create (one-time reveal of the raw
 * `pxk_...` key), list, and revoke without leaving the dashboard.
 */
export function ApiKeysPanel({ apiBaseUrl, auth }: ApiKeysPanelProps) {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [created, setCreated] = useState<CreatedApiKey | null>(null);
  const [copied, setCopied] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const data = await listApiKeys(apiBaseUrl, auth);
      setKeys(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [apiBaseUrl, auth]);

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBaseUrl]);

  function handleCreate() {
    setBusy(true);
    setError(null);
    setCopied(false);
    const trimmed = label.trim();
    void (async () => {
      try {
        const key = await createApiKey(apiBaseUrl, trimmed ? trimmed : null, auth);
        setCreated(key);
        setLabel("");
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    })();
  }

  function handleCopy() {
    if (!created) return;
    void navigator.clipboard?.writeText(created.key).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  }

  function handleRevoke(key: ApiKey) {
    if (
      !window.confirm(
        `Revoke API key "${key.label ?? key.id}"? Any client using it will stop working. This cannot be undone.`,
      )
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    void (async () => {
      try {
        await revokeApiKey(apiBaseUrl, key.id, auth);
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    })();
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
          {open ? "▾" : "▸"} <span className="eval-runner__title">API keys</span>
        </button>
        <span className="eval-runner__hint">
          Create, view, and revoke scoped API keys for programmatic access.
        </span>
      </header>

      {open ? (
        <>
          <div className="eval-runner__row">
            <label className="eval-runner__field">
              <span>Label (optional)</span>
              <input
                type="text"
                value={label}
                placeholder="e.g. ci-bot"
                onChange={(e) => setLabel(e.target.value)}
                disabled={busy}
              />
            </label>
            <div className="eval-runner__actions">
              <button
                type="button"
                className="btn primary"
                onClick={handleCreate}
                disabled={busy}
                title="Mint a new scoped API key"
              >
                {busy ? "Working…" : "Create API key"}
              </button>
            </div>
          </div>

          {created ? (
            <div className="apikey-reveal" role="status">
              <p className="apikey-reveal__warning">
                <strong>Copy this key now.</strong> This is the only time it will be
                shown — you won&apos;t be able to see it again.
              </p>
              <div className="mcp-command__row">
                <pre className="mcp-command__code">
                  <code>{created.key}</code>
                </pre>
                <button
                  type="button"
                  className="btn secondary mcp-command__copy"
                  onClick={handleCopy}
                  aria-label="Copy API key"
                >
                  {copied ? "Copied" : "Copy"}
                </button>
              </div>
              <button
                type="button"
                className="link-button"
                onClick={() => setCreated(null)}
              >
                Dismiss
              </button>
            </div>
          ) : null}

          {error ? <p className="eval-runner__error">{error}</p> : null}

          <table className="mcp-tools-table apikey-table">
            <thead>
              <tr>
                <th>Label</th>
                <th>Created</th>
                <th>Last used</th>
                <th>Status</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {keys.length === 0 ? (
                <tr>
                  <td colSpan={5} className="muted">
                    No API keys yet.
                  </td>
                </tr>
              ) : (
                keys.map((key) => (
                  <tr key={key.id}>
                    <td>{key.label ?? <span className="muted">(unlabeled)</span>}</td>
                    <td>{formatTimestamp(key.createdAt)}</td>
                    <td>{formatTimestamp(key.lastUsedAt)}</td>
                    <td>{key.revoked ? "Revoked" : "Active"}</td>
                    <td>
                      {key.revoked ? null : (
                        <button
                          type="button"
                          className="btn secondary"
                          onClick={() => handleRevoke(key)}
                          disabled={busy}
                          title="Revoke this API key"
                        >
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </>
      ) : null}
    </section>
  );
}
