import { useState } from "react";
import { useSpace } from "../../auth/SpaceGate";

const DEFAULT_OPTION = "";
const NEW_OPTION = "__new__";

/**
 * In-header dropdown for switching between a login's working spaces within the
 * active org. The first option is the default graph; picking "New space…" reveals
 * an inline create form. Selecting a space swaps the active `X-Praxis-Space` in
 * place — the data providers key on it, so the dashboard refetches automatically.
 */
export function SpaceSwitcher() {
  const { spaceId, spaces, selectSpace, createAndSelectSpace, deleteAndDeselectSpace } =
    useSpace();
  const [creating, setCreating] = useState(false);
  const [draftId, setDraftId] = useState("");
  const [draftName, setDraftName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // The label shown in the confirm prompt — the friendly name when set, else the id.
  const activeSpace = spaces.find((s) => s.spaceId === spaceId);

  function handleSelect(value: string) {
    if (value === NEW_OPTION) {
      setCreating(true);
      setError(null);
      return;
    }
    setCreating(false);
    selectSpace(value);
  }

  async function handleCreate(event: React.FormEvent) {
    event.preventDefault();
    const id = draftId.trim().toLowerCase();
    if (!id) {
      setError("Space id is required.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await createAndSelectSpace(id, draftName.trim() || undefined);
      setCreating(false);
      setDraftId("");
      setDraftName("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete() {
    if (!spaceId) {
      return;
    }
    const label =
      activeSpace?.name && activeSpace.name !== spaceId
        ? `${activeSpace.name} (${spaceId})`
        : spaceId;
    const ok = window.confirm(
      `Delete space "${label}"? This permanently removes its working knowledge ` +
        `graph (facts, snapshots, mounts). This cannot be undone. You will return ` +
        `to the Default graph.`,
    );
    if (!ok) {
      return;
    }
    setDeleting(true);
    setError(null);
    try {
      await deleteAndDeselectSpace(spaceId);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="space-switcher">
      <label className="space-switcher__label" htmlFor="space-switcher-select">
        Space
      </label>
      <div className="space-switcher__row">
        <select
          id="space-switcher-select"
          className="space-switcher__select"
          value={creating ? NEW_OPTION : spaceId}
          onChange={(e) => handleSelect(e.target.value)}
        >
          <option value={DEFAULT_OPTION}>Default graph</option>
          {spaces.map((space) => (
            <option key={space.spaceId} value={space.spaceId}>
              {space.name && space.name !== space.spaceId
                ? `${space.name} (${space.spaceId})`
                : space.spaceId}
            </option>
          ))}
          <option value={NEW_OPTION}>New space…</option>
        </select>
        {/* Delete only the currently-selected, non-default space. */}
        {spaceId && !creating ? (
          <button
            type="button"
            className="link-button link-button--danger space-switcher__delete"
            onClick={() => void handleDelete()}
            disabled={deleting}
            title="Delete this space"
          >
            {deleting ? "Deleting…" : "Delete"}
          </button>
        ) : null}
      </div>
      {error && !creating ? <p className="space-switcher__error">{error}</p> : null}

      {creating ? (
        <form className="space-switcher__create" onSubmit={handleCreate}>
          <input
            className="space-switcher__input"
            placeholder="space-id (a-z, 0-9, -, _)"
            value={draftId}
            onChange={(e) => setDraftId(e.target.value)}
            autoFocus
            required
          />
          <input
            className="space-switcher__input"
            placeholder="Name (optional)"
            value={draftName}
            onChange={(e) => setDraftName(e.target.value)}
          />
          <div className="space-switcher__actions">
            <button type="submit" disabled={submitting}>
              {submitting ? "Creating…" : "Create"}
            </button>
            <button
              type="button"
              className="link-button"
              onClick={() => {
                setCreating(false);
                setError(null);
              }}
            >
              Cancel
            </button>
          </div>
          {error ? <p className="space-switcher__error">{error}</p> : null}
        </form>
      ) : null}
    </div>
  );
}
