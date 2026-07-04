import { useState } from "react";
import { useOrg } from "../../auth/OrgGate";
import { InlineRename } from "./InlineRename";

/**
 * In-header dropdown for switching between the orgs a user belongs to.
 * Selecting an org swaps the active `X-Praxis-Org` in place — the candidate
 * and graph providers key on the org id, so data refetches automatically.
 * Owners also get an inline Delete affordance for the active org, mirroring the
 * space switcher.
 */
export function OrgSwitcher() {
  const { orgId, orgs, selectOrg, deleteAndSwitchOrg, renameAndRefreshOrg } = useOrg();
  const [deleting, setDeleting] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const activeOrg = orgs.find((o) => o.orgId === orgId);
  // Rename/delete are owner-only (the server enforces this too); only show the
  // buttons when the active membership says we own it.
  const canManage = activeOrg?.role === "owner";

  // Nothing to switch between and nothing to manage — hide the control entirely.
  if (orgs.length <= 1 && !canManage) {
    return null;
  }

  async function handleDelete() {
    if (!activeOrg) {
      return;
    }
    const label =
      activeOrg.name && activeOrg.name !== activeOrg.orgId
        ? `${activeOrg.name} (${activeOrg.orgId})`
        : activeOrg.orgId;
    const ok = window.confirm(
      `Delete organization "${label}"?\n\nThis permanently deletes the org and ALL ` +
        `of its data for EVERY member — knowledge graphs, snapshots, spaces, and ` +
        `access. This cannot be undone.`,
    );
    if (!ok) {
      return;
    }
    setDeleting(true);
    setError(null);
    try {
      await deleteAndSwitchOrg(activeOrg.orgId);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="org-switcher">
      <label className="org-switcher__label" htmlFor="org-switcher-select">
        Organization
      </label>
      {renaming && activeOrg ? (
        <InlineRename
          initialValue={activeOrg.name ?? activeOrg.orgId}
          label="New organization name"
          onSubmit={async (name) => {
            await renameAndRefreshOrg(activeOrg.orgId, name);
            setRenaming(false);
          }}
          onCancel={() => setRenaming(false)}
        />
      ) : (
        <div className="org-switcher__row">
          <select
            id="org-switcher-select"
            className="org-switcher__select"
            value={orgId}
            onChange={(e) => selectOrg(e.target.value)}
          >
            {orgs.map((org) => (
              <option key={org.orgId} value={org.orgId}>
                {org.name && org.name !== org.orgId
                  ? `${org.name} (${org.orgId})`
                  : org.orgId}
              </option>
            ))}
          </select>
          {/* Rename/delete the currently-selected org, only when we own it. */}
          {canManage ? (
            <>
              <button
                type="button"
                className="link-button org-switcher__rename"
                onClick={() => {
                  setError(null);
                  setRenaming(true);
                }}
                title="Rename this organization"
                aria-label="Rename this organization"
              >
                ✎
              </button>
              <button
                type="button"
                className="link-button link-button--danger org-switcher__delete"
                onClick={() => void handleDelete()}
                disabled={deleting}
                title="Delete this organization and all its data"
              >
                {deleting ? "Deleting…" : "Delete"}
              </button>
            </>
          ) : null}
        </div>
      )}
      {error && !renaming ? <p className="space-switcher__error">{error}</p> : null}
    </div>
  );
}
