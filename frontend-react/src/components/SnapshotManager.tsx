import { useCallback, useEffect, useState } from "react";
import {
  type ApiDataProviderAuth,
  deleteSnapshot,
  listSnapshots,
  loadSnapshot,
  saveSnapshot,
} from "../api/apiClient";
import type { Snapshot } from "../api/dataProvider";

interface SnapshotManagerProps {
  apiBaseUrl: string;
  auth?: string | ApiDataProviderAuth;
  /** Called after a destructive load so the dashboard can refresh candidates/graph. */
  onLoaded?: () => void;
}

/** Save the live graph as a named snapshot, or restore one (destructive). */
export function SnapshotManager({ apiBaseUrl, auth, onLoaded }: SnapshotManagerProps) {
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const data = await listSnapshots(apiBaseUrl, auth);
      setSnapshots(data);
      setSelected((prev) =>
        prev && data.some((s) => s.name === prev) ? prev : (data[0]?.name ?? ""),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [apiBaseUrl, auth]);

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBaseUrl]);

  async function run(action: () => Promise<string | null>) {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const result = await action();
      if (result) setMessage(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  function handleCreate() {
    const name = window.prompt("Name for the new snapshot:")?.trim();
    if (!name) return;
    void run(async () => {
      const saved = await saveSnapshot(apiBaseUrl, name, auth);
      await refresh();
      setSelected(saved.name);
      return `Saved snapshot "${saved.name}" (${saved.count} nodes).`;
    });
  }

  function handleOverwrite() {
    if (!selected) return;
    if (!window.confirm(`Overwrite snapshot "${selected}" with the current live graph?`)) {
      return;
    }
    void run(async () => {
      const saved = await saveSnapshot(apiBaseUrl, selected, auth);
      await refresh();
      return `Overwrote snapshot "${saved.name}" (${saved.count} nodes).`;
    });
  }

  function handleAdd() {
    if (!selected) return;
    void run(async () => {
      const result = await loadSnapshot(apiBaseUrl, selected, "add", auth);
      onLoaded?.();
      return `Added snapshot "${selected}" (${result.loaded} nodes) to the live graph.`;
    });
  }

  function handleReplace() {
    if (!selected) return;
    if (
      !window.confirm(
        `Replace graph with snapshot "${selected}"? This is destructive: it clears the live graph and replaces it with the snapshot.`,
      )
    ) {
      return;
    }
    void run(async () => {
      const result = await loadSnapshot(apiBaseUrl, selected, "replace", auth);
      onLoaded?.();
      return `Loaded snapshot "${selected}" (${result.loaded} nodes) into the live graph.`;
    });
  }

  function handleDelete() {
    if (!selected) return;
    if (!window.confirm(`Delete snapshot "${selected}"? This cannot be undone.`)) {
      return;
    }
    void run(async () => {
      await deleteSnapshot(apiBaseUrl, selected, auth);
      const name = selected;
      await refresh();
      return `Deleted snapshot "${name}".`;
    });
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
          {open ? "▾" : "▸"} <span className="eval-runner__title">Snapshots</span>
        </button>
        <span className="eval-runner__hint">
          Save the current live graph, or restore a saved copy (replaces the live graph).
        </span>
      </header>

      {open ? (
        <>
          <div className="eval-runner__row">
            <label className="eval-runner__field">
              <span>Saved snapshots</span>
              <select
                value={selected}
                onChange={(e) => setSelected(e.target.value)}
                disabled={busy || snapshots.length === 0}
              >
                {snapshots.length === 0 ? (
                  <option value="">No snapshots saved</option>
                ) : (
                  snapshots.map((s) => (
                    <option key={s.name} value={s.name}>
                      {s.name} ({s.count} nodes)
                    </option>
                  ))
                )}
              </select>
            </label>
          </div>
          <div className="eval-runner__row">
            <div className="eval-runner__actions">
              <button
                type="button"
                className="btn primary"
                onClick={handleCreate}
                disabled={busy}
                title="Save the current live graph under a new name"
              >
                Create snapshot
              </button>
              <button
                type="button"
                className="btn secondary"
                onClick={handleOverwrite}
                disabled={busy || !selected}
                title="Replace the selected snapshot with the current live graph"
              >
                Overwrite
              </button>
              <button
                type="button"
                className="btn secondary"
                onClick={handleAdd}
                disabled={busy || !selected}
                title="Add this snapshot's nodes to the current graph"
                aria-label="Add this snapshot's nodes to the current graph"
              >
                {busy ? "Working…" : "Add"}
              </button>
              <button
                type="button"
                className="btn secondary"
                onClick={handleReplace}
                disabled={busy || !selected}
                title="Destructive: clear the live graph and insert this snapshot"
              >
                Replace graph
              </button>
              <button
                type="button"
                className="btn secondary"
                onClick={handleDelete}
                disabled={busy || !selected}
                title="Delete the selected snapshot"
              >
                Delete
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
