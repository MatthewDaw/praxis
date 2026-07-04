import { useCallback, useEffect, useState } from "react";
import {
  type ApiDataProviderAuth,
  deleteSnapshot,
  listSnapshots,
  loadSnapshot,
  saveSnapshot,
} from "../../api/apiClient";
import type { Snapshot } from "../../api/dataProvider";
import { Modal } from "./Modal";

// Sentinel <option> value for the "create a new snapshot" choice — distinct from
// any real snapshot name (which can never be empty or start with these chars).
const NEW_SNAPSHOT_VALUE = "__new_snapshot__";

interface SnapshotSwitcherProps {
  apiBaseUrl: string;
  auth?: string | ApiDataProviderAuth;
  /** The snapshot the live graph was last loaded from / saved to ("" = none). */
  activeSnapshot: string;
  /** True when the live graph has edits since the active snapshot was loaded. */
  dirty: boolean;
  /**
   * Mark the live graph as in-sync with snapshot `name` — App persists it as the
   * active snapshot and clears the dirty flag. Pass the same active name to record
   * a save-in-place; pass a new name to record a switch.
   */
  onSynced: (name: string) => void;
  /** Refetch candidates + graph after a destructive load replaced the live graph. */
  onGraphReplaced: () => void;
}

/**
 * Header dropdown for quickly switching the live graph between saved snapshots,
 * sitting directly below the space switcher. Selecting a snapshot loads it
 * destructively (replace). If the graph has unsaved edits (the "pending save"
 * light is lit), a confirm popup first offers to cancel, discard, or save the
 * current graph back into the active snapshot before switching.
 */
export function SnapshotSwitcher({
  apiBaseUrl,
  auth,
  activeSnapshot,
  dirty,
  onSynced,
  onGraphReplaced,
}: SnapshotSwitcherProps) {
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The snapshot the user picked while the graph was dirty — awaiting a confirm choice.
  const [pendingTarget, setPendingTarget] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setSnapshots(await listSnapshots(apiBaseUrl, auth));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [apiBaseUrl, auth]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  // Destructively load `target` into the live graph and record it as the new active.
  function load(target: string) {
    return run(async () => {
      await loadSnapshot(apiBaseUrl, target, "replace", auth);
      onSynced(target);
      onGraphReplaced();
    });
  }

  // Prompt for a name and save the current live graph as a brand-new snapshot,
  // making it the active snapshot.
  function handleCreateNew() {
    const entered = window.prompt("Save the current graph as a new snapshot named:")?.trim();
    if (!entered) return;
    void run(async () => {
      await saveSnapshot(apiBaseUrl, entered, auth);
      onSynced(entered);
      await refresh();
    });
  }

  function handleSelect(value: string) {
    if (value === NEW_SNAPSHOT_VALUE) {
      handleCreateNew();
      return;
    }
    if (!value || value === activeSnapshot) return;
    // A clean graph has nothing to lose — switch straight away. A dirty graph
    // routes through the confirm popup first.
    if (dirty) {
      setPendingTarget(value);
      return;
    }
    void load(value);
  }

  function handleSaveInPlace() {
    if (!activeSnapshot) return;
    void run(async () => {
      await saveSnapshot(apiBaseUrl, activeSnapshot, auth);
      onSynced(activeSnapshot);
      await refresh();
    });
  }

  function handleDelete() {
    if (!activeSnapshot) return;
    if (!window.confirm(`Delete snapshot "${activeSnapshot}"? This cannot be undone.`)) {
      return;
    }
    void run(async () => {
      await deleteSnapshot(apiBaseUrl, activeSnapshot, auth);
      // The live graph is untouched by deleting its saved copy — just drop the
      // tracked active snapshot (and its pending-save baseline) and refresh.
      onSynced("");
      await refresh();
    });
  }

  function handleDiscardAndSwitch() {
    const target = pendingTarget;
    setPendingTarget(null);
    if (target) void load(target);
  }

  function handleSaveAndSwitch() {
    const target = pendingTarget;
    if (!target) return;
    // Overwrite the snapshot the graph currently belongs to; if none is tracked,
    // fall back to a name prompt so the unsaved work still has somewhere to land.
    let saveName = activeSnapshot;
    if (!saveName) {
      const entered = window.prompt("Save the current graph as a snapshot named:")?.trim();
      if (!entered) return; // cancelled the name prompt — keep the popup open
      saveName = entered;
    }
    setPendingTarget(null);
    void run(async () => {
      await saveSnapshot(apiBaseUrl, saveName, auth);
      await loadSnapshot(apiBaseUrl, target, "replace", auth);
      onSynced(target);
      onGraphReplaced();
      await refresh();
    });
  }

  // Ensure the active snapshot always has a matching <option>, even if it was
  // deleted elsewhere or the list has not loaded yet, so the select shows it.
  const hasActiveOption = !activeSnapshot || snapshots.some((s) => s.name === activeSnapshot);

  return (
    <div className="snapshot-switcher space-switcher">
      <label className="space-switcher__label" htmlFor="snapshot-switcher-select">
        Snapshot
      </label>
      <div className="space-switcher__row">
        <select
          id="snapshot-switcher-select"
          className="space-switcher__select"
          value={activeSnapshot}
          onChange={(e) => handleSelect(e.target.value)}
          disabled={busy}
        >
          <option value="">
            {activeSnapshot ? "Switch to snapshot…" : "No snapshot loaded"}
          </option>
          {!hasActiveOption ? (
            <option value={activeSnapshot}>{activeSnapshot} (not saved)</option>
          ) : null}
          {snapshots.map((s) => (
            <option key={s.name} value={s.name}>
              {s.name} ({s.count} nodes)
            </option>
          ))}
          <option value={NEW_SNAPSHOT_VALUE}>New snapshot…</option>
        </select>
        {activeSnapshot ? (
          <button
            type="button"
            className="link-button link-button--danger space-switcher__delete"
            onClick={handleDelete}
            disabled={busy}
            title="Delete this snapshot"
          >
            Delete
          </button>
        ) : null}
      </div>

      <div className="snapshot-switcher__status" aria-live="polite">
        {dirty ? (
          <>
            <span
              className="snapshot-switcher__pending"
              title="The live graph has edits not yet saved to its snapshot"
            >
              <span className="snapshot-switcher__dot" aria-hidden="true" />
              Unsaved changes
            </span>
            {activeSnapshot ? (
              <button
                type="button"
                className="btn secondary snapshot-switcher__save"
                onClick={handleSaveInPlace}
                disabled={busy}
                title={`Save the live graph into "${activeSnapshot}"`}
              >
                Save
              </button>
            ) : null}
          </>
        ) : activeSnapshot ? (
          <span className="snapshot-switcher__synced">
            <span
              className="snapshot-switcher__dot snapshot-switcher__dot--ok"
              aria-hidden="true"
            />
            Saved to “{activeSnapshot}”
          </span>
        ) : null}
      </div>

      {error ? <p className="space-switcher__error">{error}</p> : null}

      {pendingTarget ? (
        <Modal title="Unsaved changes" onClose={() => setPendingTarget(null)}>
          <div className="snapshot-switcher__confirm">
            <p>
              The live graph has unsaved edits
              {activeSnapshot ? (
                <>
                  {" "}
                  since you loaded <strong>{activeSnapshot}</strong>
                </>
              ) : null}
              . Loading <strong>{pendingTarget}</strong> replaces the current graph.
            </p>
            <div className="snapshot-switcher__confirm-actions">
              <button
                type="button"
                className="btn secondary"
                onClick={() => setPendingTarget(null)}
                disabled={busy}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn secondary"
                onClick={handleDiscardAndSwitch}
                disabled={busy}
                title="Discard the current graph and load the selected snapshot"
              >
                Discard &amp; switch
              </button>
              <button
                type="button"
                className="btn primary"
                onClick={handleSaveAndSwitch}
                disabled={busy}
                title={
                  activeSnapshot
                    ? `Save the graph into "${activeSnapshot}", then load "${pendingTarget}"`
                    : `Save the graph as a new snapshot, then load "${pendingTarget}"`
                }
              >
                {activeSnapshot ? "Save & switch" : "Save as… & switch"}
              </button>
            </div>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
