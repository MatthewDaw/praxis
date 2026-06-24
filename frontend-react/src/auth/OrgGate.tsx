import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { signOut as amplifySignOut } from "aws-amplify/auth";
import { contractHeaders } from "../api/contract";
import { resolveInitialConfig } from "../config/dataSource";
import { useAuthToken } from "./useAuthToken";

const ACTIVE_ORG_STORAGE_KEY = "praxis-active-org";

export interface OrgMembership {
  orgId: string;
  name?: string;
  role?: string;
}

export interface MeResponse {
  sub: string;
  email?: string;
  orgs: OrgMembership[];
}

export interface OrgContextValue {
  /** The active org id (sent as X-Praxis-Org on data calls). */
  orgId: string;
  /** The active org's display name, when one is set (falls back to orgId). */
  orgName?: string;
  /** The logged-in user's id (Cognito sub). */
  userId: string;
  /** The logged-in user's email, when present on the token. */
  email?: string;
  /** Resolve a currently-valid Cognito ID token. */
  getToken: () => Promise<string | undefined>;
  /** Sign the user out of Amplify. */
  signOut: () => Promise<void>;
  /** Clear the active org and return to the workspace picker (stays logged in). */
  switchOrg: () => void;
}

const OrgContext = createContext<OrgContextValue | null>(null);

export function useOrg(): OrgContextValue {
  const ctx = useContext(OrgContext);
  if (!ctx) {
    throw new Error("useOrg must be used within <OrgGate>");
  }
  return ctx;
}

/** Resolve the live API base url that org/auth calls target. */
export function orgApiBaseUrl(): string {
  return resolveInitialConfig().apiBaseUrl ?? "http://localhost:8000";
}

function normalizeMemberships(payload: unknown): OrgMembership[] {
  if (!payload || typeof payload !== "object") {
    return [];
  }
  const orgs = (payload as { orgs?: unknown }).orgs;
  if (!Array.isArray(orgs)) {
    return [];
  }
  const result: OrgMembership[] = [];
  for (const entry of orgs) {
    if (!entry || typeof entry !== "object") {
      continue;
    }
    const record = entry as Record<string, unknown>;
    const orgId = (record.orgId ?? record.org_id) as string | undefined;
    if (!orgId) {
      continue;
    }
    result.push({
      orgId,
      name: record.name as string | undefined,
      role: record.role as string | undefined,
    });
  }
  return result;
}

export async function fetchMe(
  baseUrl: string,
  getToken: () => Promise<string | undefined>,
): Promise<MeResponse> {
  const token = await getToken();
  const response = await fetch(`${baseUrl.replace(/\/$/, "")}/me`, {
    headers: contractHeaders(token),
  });
  if (!response.ok) {
    throw new Error(`GET /me failed (${response.status})`);
  }
  const payload = (await response.json()) as Record<string, unknown>;
  return {
    sub: (payload.sub as string) ?? "",
    email: payload.email as string | undefined,
    orgs: normalizeMemberships(payload),
  };
}

export async function createOrg(
  baseUrl: string,
  getToken: () => Promise<string | undefined>,
  body: { orgId: string; name: string; password: string },
): Promise<void> {
  const token = await getToken();
  const response = await fetch(`${baseUrl.replace(/\/$/, "")}/orgs`, {
    method: "POST",
    headers: contractHeaders(token),
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `POST /orgs failed (${response.status})`);
  }
}

export async function joinOrg(
  baseUrl: string,
  getToken: () => Promise<string | undefined>,
  body: { orgId: string; password: string },
): Promise<void> {
  const token = await getToken();
  const response = await fetch(`${baseUrl.replace(/\/$/, "")}/orgs/join`, {
    method: "POST",
    headers: contractHeaders(token),
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `POST /orgs/join failed (${response.status})`);
  }
}

export async function changeOrgPassword(
  baseUrl: string,
  getToken: () => Promise<string | undefined>,
  body: { orgId: string; currentPassword: string; newPassword: string },
): Promise<void> {
  const token = await getToken();
  const response = await fetch(`${baseUrl.replace(/\/$/, "")}/orgs/password`, {
    method: "POST",
    headers: contractHeaders(token),
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `POST /orgs/password failed (${response.status})`);
  }
}

interface OrgGateProps {
  children: ReactNode;
}

export function OrgGate({ children }: OrgGateProps) {
  const { getToken } = useAuthToken();
  const baseUrl = useMemo(() => orgApiBaseUrl(), []);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [memberships, setMemberships] = useState<OrgMembership[]>([]);
  const [identity, setIdentity] = useState<{ sub: string; email?: string }>({ sub: "" });
  const [activeOrg, setActiveOrg] = useState<string | null>(() =>
    localStorage.getItem(ACTIVE_ORG_STORAGE_KEY),
  );

  const [createForm, setCreateForm] = useState({ orgId: "", name: "", password: "" });
  const [joinForm, setJoinForm] = useState({ orgId: "", password: "" });
  const [pwForm, setPwForm] = useState({ orgId: "", currentPassword: "", newPassword: "" });
  const [notice, setNotice] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const loadMe = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const me = await fetchMe(baseUrl, getToken);
      setMemberships(me.orgs);
      setIdentity({ sub: me.sub, email: me.email });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setMemberships([]);
    } finally {
      setLoading(false);
    }
  }, [baseUrl, getToken]);

  useEffect(() => {
    void loadMe();
  }, [loadMe]);

  const chooseOrg = useCallback((orgId: string) => {
    localStorage.setItem(ACTIVE_ORG_STORAGE_KEY, orgId);
    setActiveOrg(orgId);
  }, []);

  const handleSignOut = useCallback(async () => {
    localStorage.removeItem(ACTIVE_ORG_STORAGE_KEY);
    await amplifySignOut();
  }, []);

  const switchOrg = useCallback(() => {
    localStorage.removeItem(ACTIVE_ORG_STORAGE_KEY);
    setActiveOrg(null);
  }, []);

  async function handleCreate(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await createOrg(baseUrl, getToken, createForm);
      chooseOrg(createForm.orgId);
      await loadMe();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleJoin(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await joinOrg(baseUrl, getToken, joinForm);
      chooseOrg(joinForm.orgId);
      await loadMe();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleChangePassword(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    setNotice(null);
    try {
      await changeOrgPassword(baseUrl, getToken, pwForm);
      setNotice(`Password changed for ${pwForm.orgId}.`);
      setPwForm({ orgId: "", currentPassword: "", newPassword: "" });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  const contextValue = useMemo<OrgContextValue | null>(() => {
    if (!activeOrg) {
      return null;
    }
    // Only expose a valid active org that the user is actually a member of
    // (a stale localStorage value should not unlock the app).
    if (memberships.length > 0 && !memberships.some((m) => m.orgId === activeOrg)) {
      return null;
    }
    const orgName = memberships.find((m) => m.orgId === activeOrg)?.name;
    return {
      orgId: activeOrg,
      orgName,
      userId: identity.sub,
      email: identity.email,
      getToken,
      signOut: handleSignOut,
      switchOrg,
    };
  }, [activeOrg, memberships, identity, getToken, handleSignOut, switchOrg]);

  if (loading) {
    return <div className="org-gate org-gate--loading">Loading your workspace…</div>;
  }

  if (contextValue) {
    return <OrgContext.Provider value={contextValue}>{children}</OrgContext.Provider>;
  }

  return (
    <div className="org-gate">
      <header className="org-gate__header">
        <h1>Choose a workspace</h1>
        <button type="button" className="link-button" onClick={() => void handleSignOut()}>
          Sign out
        </button>
      </header>

      {error ? <div className="error-banner">{error}</div> : null}
      {notice ? <div className="success-banner">{notice}</div> : null}

      {memberships.length > 0 ? (
        <section className="org-gate__pick">
          <h2>Your organizations</h2>
          <ul>
            {memberships.map((m) => (
              <li key={m.orgId}>
                <button type="button" onClick={() => chooseOrg(m.orgId)}>
                  {m.name ? `${m.name} (${m.orgId})` : m.orgId}
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : (
        <p className="org-gate__muted">
          You are not a member of any organization yet. Create one or join with a password.
        </p>
      )}

      <section className="org-gate__create">
        <h2>Create an organization</h2>
        <form onSubmit={handleCreate}>
          <input
            placeholder="Org id"
            value={createForm.orgId}
            onChange={(e) => setCreateForm({ ...createForm, orgId: e.target.value })}
            required
          />
          <input
            placeholder="Name"
            value={createForm.name}
            onChange={(e) => setCreateForm({ ...createForm, name: e.target.value })}
          />
          <input
            type="password"
            placeholder="Password"
            value={createForm.password}
            onChange={(e) => setCreateForm({ ...createForm, password: e.target.value })}
            required
          />
          <button type="submit" disabled={submitting}>
            Create org
          </button>
        </form>
      </section>

      <section className="org-gate__join">
        <h2>Join an organization</h2>
        <form onSubmit={handleJoin}>
          <input
            placeholder="Org id"
            value={joinForm.orgId}
            onChange={(e) => setJoinForm({ ...joinForm, orgId: e.target.value })}
            required
          />
          <input
            type="password"
            placeholder="Password"
            value={joinForm.password}
            onChange={(e) => setJoinForm({ ...joinForm, password: e.target.value })}
            required
          />
          <button type="submit" disabled={submitting}>
            Join org
          </button>
        </form>
      </section>

      <section className="org-gate__password">
        <h2>Change an organization's password</h2>
        <form onSubmit={handleChangePassword}>
          <input
            placeholder="Org id"
            value={pwForm.orgId}
            onChange={(e) => setPwForm({ ...pwForm, orgId: e.target.value })}
            required
          />
          <input
            type="password"
            placeholder="Current password"
            value={pwForm.currentPassword}
            onChange={(e) => setPwForm({ ...pwForm, currentPassword: e.target.value })}
            required
          />
          <input
            type="password"
            placeholder="New password"
            value={pwForm.newPassword}
            onChange={(e) => setPwForm({ ...pwForm, newPassword: e.target.value })}
            required
          />
          <button type="submit" disabled={submitting}>
            Change password
          </button>
        </form>
      </section>
    </div>
  );
}
