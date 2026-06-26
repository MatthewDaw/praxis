import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { createSpace, deleteSpace, listSpaces, type Space } from "../api/spaces";
import { orgApiBaseUrl, useOrg } from "./OrgGate";

/**
 * Active-space context layered on top of {@link OrgGate}. A *space* is a login's
 * private, named working knowledge graph within the current org; selecting one
 * makes every data call send `X-Praxis-Space`, so the dashboard reads/writes a
 * sibling graph without changing org or login. The empty space id is the default
 * graph (no header), so the control is always safe to ignore.
 *
 * Selection is persisted per-org (`praxis-active-space:<orgId>`): switching orgs
 * never carries a space id that does not exist under the new org.
 */
export interface SpaceContextValue {
  /** The active space id (sent as X-Praxis-Space); "" is the default graph. */
  spaceId: string;
  /** The spaces the user owns in the active org (powers the switcher). */
  spaces: Space[];
  /** Switch the active space in place; "" returns to the default graph. */
  selectSpace: (spaceId: string) => void;
  /** Create a space in the active org, then select it. */
  createAndSelectSpace: (spaceId: string, name?: string) => Promise<void>;
  /**
   * Permanently delete a space, refresh the list, and — if the deleted space was
   * the active one — fall back to the default graph (selectSpace("")).
   */
  deleteAndDeselectSpace: (spaceId: string) => Promise<void>;
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
  const { orgId, getToken } = useOrg();
  const baseUrl = useMemo(() => orgApiBaseUrl(), []);

  const [spaces, setSpaces] = useState<Space[]>([]);
  const [spaceId, setSpaceId] = useState<string>(
    () => localStorage.getItem(storageKey(orgId)) ?? "",
  );

  // Re-read the persisted selection whenever the active org changes — a space id
  // is meaningful only within its org.
  useEffect(() => {
    setSpaceId(localStorage.getItem(storageKey(orgId)) ?? "");
  }, [orgId]);

  const refreshSpaces = useCallback(async () => {
    try {
      setSpaces(await listSpaces(baseUrl, getToken, orgId));
    } catch {
      // A spaces endpoint that is unavailable (older backend) or errors should
      // not break the dashboard — the default graph still works with no header.
      setSpaces([]);
    }
  }, [baseUrl, getToken, orgId]);

  useEffect(() => {
    void refreshSpaces();
  }, [refreshSpaces]);

  // A persisted space the user no longer owns (deleted, or stale) falls back to
  // the default graph rather than sending a header the backend would 404.
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
      // If the graph the user was viewing just vanished, return to the default.
      if (targetSpaceId === spaceId) {
        selectSpace("");
      }
    },
    [baseUrl, getToken, orgId, refreshSpaces, selectSpace, spaceId],
  );

  const value = useMemo<SpaceContextValue>(
    () => ({ spaceId, spaces, selectSpace, createAndSelectSpace, deleteAndDeselectSpace }),
    [spaceId, spaces, selectSpace, createAndSelectSpace, deleteAndDeselectSpace],
  );

  return <SpaceContext.Provider value={value}>{children}</SpaceContext.Provider>;
}
