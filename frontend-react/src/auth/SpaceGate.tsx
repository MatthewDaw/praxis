import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { createSpace, deleteSpace, listSpaces, renameSpace, type Space } from "../api/spaces";
import { orgApiBaseUrl, useOrg } from "./OrgGate";

/**
 * Active-space context layered on top of {@link OrgGate}. A *space* is a named
 * container for the org-shared snapshots the login saves/loads within the current
 * org. A space MUST be selected before the dashboard renders: {@link SpaceGate}
 * blocks on a valid selection and shows a create/pick screen otherwise, so there
 * is no "default graph" state in which snapshot operations have nowhere to land.
 *
 * Note: the backend keys a login's *working memory* on `(org, sub)` alone — the
 * `X-Praxis-Space` header only selects a snapshot folder (paired with
 * `X-Praxis-Snapshot`), it does NOT scope the live facts graph. Switching spaces
 * therefore changes which snapshots you save into, not which facts you see.
 *
 * Selection is persisted per-org (`praxis-active-space:<orgId>`): switching orgs
 * never carries a space id that does not exist under the new org.
 */
export interface SpaceContextValue {
  /** The active space id (sent as X-Praxis-Space); always a real, owned space. */
  spaceId: string;
  /** The spaces the user owns in the active org (powers the switcher). */
  spaces: Space[];
  /** Switch the active space in place. */
  selectSpace: (spaceId: string) => void;
  /** Create a space in the active org, then select it. */
  createAndSelectSpace: (spaceId: string, name?: string) => Promise<void>;
  /**
   * Permanently delete a space, refresh the list, and — if the deleted space was
   * the active one — drop back to the space picker (selectSpace("")).
   */
  deleteAndDeselectSpace: (spaceId: string) => Promise<void>;
  /** Rename a space's display name (the spaceId key is unchanged) and refresh. */
  renameAndRefreshSpace: (spaceId: string, name: string) => Promise<void>;
}

const SpaceContext = createContext<SpaceContextValue | null>(null);

export function useSpace(): SpaceContextValue {
  const ctx = useContext(SpaceContext);
  if (!ctx) {
    throw new Error("useSpace must be used within <SpaceGate>");
  }
  return ctx;
}

function storageKey(orgId: string): string {
  return `praxis-active-space:${orgId}`;
}

interface SpaceGateProps {
  children: ReactNode;
}

export function SpaceGate({ children }: SpaceGateProps) {
  const { orgId, orgName, getToken, switchOrg, signOut } = useOrg();
  const baseUrl = useMemo(() => orgApiBaseUrl(), []);

  const [spaces, setSpaces] = useState<Space[]>([]);
  const [loading, setLoading] = useState(true);
  const [spaceId, setSpaceId] = useState<string>(
    () => localStorage.getItem(storageKey(orgId)) ?? "",
  );

  // Create-form state for the gate screen (when no space is selected yet).
  const [draftId, setDraftId] = useState("");
  const [draftName, setDraftName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [gateError, setGateError] = useState<string | null>(null);

  // Re-read the persisted selection whenever the active org changes — a space id
  // is meaningful only within its org.
  useEffect(() => {
    setSpaceId(localStorage.getItem(storageKey(orgId)) ?? "");
  }, [orgId]);

  const refreshSpaces = useCallback(async () => {
    setLoading(true);
    try {
      setSpaces(await listSpaces(baseUrl, getToken, orgId));
    } catch {
      // A spaces endpoint that is unavailable (older backend) or errors leaves an
      // empty list — the gate then forces the user to create their first space.
      setSpaces([]);
    } finally {
      setLoading(false);
    }
  }, [baseUrl, getToken, orgId]);

  useEffect(() => {
    void refreshSpaces();
  }, [refreshSpaces]);

  // A persisted space the user no longer owns (deleted, or stale) falls back to
  // the gate rather than sending a header the backend would 404.
  useEffect(() => {
    if (spaceId && spaces.length > 0 && !spaces.some((s) => s.spaceId === spaceId)) {
      setSpaceId("");
      localStorage.removeItem(storageKey(orgId));
    }
  }, [spaceId, spaces, orgId]);

  const selectSpace = useCallback(
    (next: string) => {
      setSpaceId(next);
      if (next) {
        localStorage.setItem(storageKey(orgId), next);
      } else {
        localStorage.removeItem(storageKey(orgId));
      }
    },
    [orgId],
  );

  const createAndSelectSpace = useCallback(
    async (newSpaceId: string, name?: string) => {
      await createSpace(baseUrl, getToken, orgId, { spaceId: newSpaceId, name });
      await refreshSpaces();
      selectSpace(newSpaceId);
    },
    [baseUrl, getToken, orgId, refreshSpaces, selectSpace],
  );

  const deleteAndDeselectSpace = useCallback(
    async (targetSpaceId: string) => {
      await deleteSpace(baseUrl, getToken, orgId, targetSpaceId);
      await refreshSpaces();
      // If the graph the user was viewing just vanished, return to the picker.
      if (targetSpaceId === spaceId) {
        selectSpace("");
      }
    },
    [baseUrl, getToken, orgId, refreshSpaces, selectSpace, spaceId],
  );

  const renameAndRefreshSpace = useCallback(
    async (targetSpaceId: string, name: string) => {
      await renameSpace(baseUrl, getToken, orgId, targetSpaceId, name);
      await refreshSpaces();
    },
    [baseUrl, getToken, orgId, refreshSpaces],
  );

  const value = useMemo<SpaceContextValue>(
    () => ({
      spaceId,
      spaces,
      selectSpace,
      createAndSelectSpace,
      deleteAndDeselectSpace,
      renameAndRefreshSpace,
    }),
    [
      spaceId,
      spaces,
      selectSpace,
      createAndSelectSpace,
      deleteAndDeselectSpace,
      renameAndRefreshSpace,
    ],
  );

  const activeValid = spaceId !== "" && spaces.some((s) => s.spaceId === spaceId);

  async function handleGateCreate(event: React.FormEvent) {
    event.preventDefault();
    const id = draftId.trim().toLowerCase();
    if (!id) {
      setGateError("Space id is required.");
      return;
    }
    setSubmitting(true);
    setGateError(null);
    try {
      await createAndSelectSpace(id, draftName.trim() || undefined);
      setDraftId("");
      setDraftName("");
    } catch (err) {
      setGateError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) {
    return <div className="org-gate org-gate--loading">Loading your spaces…</div>;
  }

  // Gate: a real space must be selected before the dashboard renders. There is no
  // "default graph" — snapshots live inside a space, so one is always required.
  if (!activeValid) {
    const orgLabel = orgName && orgName !== orgId ? `${orgName} (${orgId})` : orgId;
    return (
      <div className="org-gate">
        <header className="org-gate__header">
          <h1>Choose a space</h1>
          <button type="button" className="link-button" onClick={() => void signOut()}>
            Sign out
          </button>
        </header>

        <p className="muted">
          A space holds your saved snapshots in <strong>{orgLabel}</strong>. Pick one
          or create one to open the dashboard.
        </p>

        {gateError ? <div className="error-banner">{gateError}</div> : null}

        {spaces.length > 0 ? (
          <section className="org-gate__pick">
            <h2>Your spaces</h2>
            <ul>
              {spaces.map((s) => (
                <li key={s.spaceId} className="org-gate__pick-row">
                  <button type="button" onClick={() => selectSpace(s.spaceId)}>
                    {s.name && s.name !== s.spaceId ? `${s.name} (${s.spaceId})` : s.spaceId}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        <section className="org-gate__create">
          <h2>{spaces.length > 0 ? "Or create a new space" : "Create your first space"}</h2>
          <form className="space-switcher__create" onSubmit={handleGateCreate}>
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
                {submitting ? "Creating…" : "Create & open"}
              </button>
            </div>
          </form>
        </section>

        <footer className="org-gate__footer">
          <button type="button" className="link-button" onClick={switchOrg}>
            Switch workspace
          </button>
        </footer>
      </div>
    );
  }

  return <SpaceContext.Provider value={value}>{children}</SpaceContext.Provider>;
}
