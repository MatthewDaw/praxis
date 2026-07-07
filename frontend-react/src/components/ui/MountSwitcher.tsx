import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  type ApiDataProviderAuth,
  type Mount,
  type OrgSource,
  listMounts,
  listOrgSources,
  mountSnapshot,
  unmountSnapshot,
} from "../../api/apiClient";

interface MountSwitcherProps {
  apiBaseUrl: string;
  auth?: string | ApiDataProviderAuth;
}

/** A pickable snapshot across the org: own snapshots and teammates'. */
interface SnapshotOption {
  /** Stable identity: `${userId}::${snapshot}` — matches a Mount by source + name. */
  key: string;
  userId: string;
  /** Display name of the owner (email or id); "me" for the caller's own. */
  owner: string;
  snapshot: string;
  count: number;
  isSelf: boolean;
}

const MAX_RESULTS = 8;

/** Flatten every org member's saved snapshots into one searchable option list. */
function buildOptions(sources: OrgSource[]): SnapshotOption[] {
  const options: SnapshotOption[] = [];
  for (const s of sources) {
    const owner = s.isSelf ? "me" : s.username || s.userId;
    for (const snap of s.snapshots) {
      options.push({
        key: `${s.userId}::${snap.name}`,
        userId: s.userId,
        owner,
        snapshot: snap.name,
        count: snap.count,
        isSelf: s.isSelf,
      });
    }
  }
  return options;
}

/** The Mount-list key for an option, so we can tell which options are mounted. */
function mountKey(sourceUser: string, snapshot: string): string {
  return `${sourceUser}::${snapshot}`;
}

/**
 * Always-visible header control for the "extra snapshots" the live graph reads
 * from (mounts) without merging them in. Mounted snapshots show as removable
 * chips (one click to drop); a search box lists every org snapshot you can add,
 * one click to mount. Reads union these overlays in; saves never carry them.
 */
export function MountSwitcher({ apiBaseUrl, auth }: MountSwitcherProps) {
  const [mounts, setMounts] = useState<Mount[]>([]);
  const [options, setOptions] = useState<SnapshotOption[]>([]);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Snapshot keys with an in-flight mount/unmount, so their row shows a spinner
  // and can't be double-clicked.
  const [pending, setPending] = useState<Set<string>>(new Set());
  const blurTimer = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [mountData, sources] = await Promise.all([
        listMounts(apiBaseUrl, auth),
        listOrgSources(apiBaseUrl, auth).catch(() => [] as OrgSource[]),
      ]);
      setMounts(mountData);
      setOptions(buildOptions(sources));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [apiBaseUrl, auth]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    return () => {
      if (blurTimer.current) window.clearTimeout(blurTimer.current);
    };
  }, []);

  const mountedKeys = useMemo(
    () => new Set(mounts.map((m) => mountKey(m.sourceUser, m.snapshot))),
    [mounts],
  );

  // Options that match the search box, mounted ones first so adding feels stable.
  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matched = options.filter(
      (o) =>
        !q ||
        o.snapshot.toLowerCase().includes(q) ||
        o.owner.toLowerCase().includes(q),
    );
    return matched.slice(0, MAX_RESULTS);
  }, [options, query]);

  async function run(key: string, action: () => Promise<void>) {
    setPending((prev) => new Set(prev).add(key));
    setError(null);
    try {
      await action();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending((prev) => {
        const next = new Set(prev);
        next.delete(key);
        return next;
      });
    }
  }

  function add(option: SnapshotOption) {
    void run(option.key, () =>
      mountSnapshot(apiBaseUrl, option.snapshot, option.isSelf ? undefined : option.userId, auth),
    );
  }

  function remove(m: Mount) {
    void run(mountKey(m.sourceUser, m.snapshot), () =>
      unmountSnapshot(apiBaseUrl, m.snapshot, m.isSelf ? undefined : m.sourceUser, auth),
    );
  }

  function toggle(option: SnapshotOption) {
    if (mountedKeys.has(option.key)) {
      void run(option.key, () =>
        unmountSnapshot(
          apiBaseUrl,
          option.snapshot,
          option.isSelf ? undefined : option.userId,
          auth,
        ),
      );
      return;
    }
    add(option);
  }

  // Delay close so a click on a result registers before the list unmounts.
  function handleBlur() {
    blurTimer.current = window.setTimeout(() => setOpen(false), 150);
  }
  function handleFocus() {
    if (blurTimer.current) window.clearTimeout(blurTimer.current);
    setOpen(true);
  }

  return (
    <section className="mount-switcher">
      <button
        type="button"
        className="mount-switcher__bar"
        onClick={() => setCollapsed((v) => !v)}
        aria-expanded={!collapsed}
        title={collapsed ? "Expand" : "Collapse"}
      >
        <span className="mount-switcher__chevron" aria-hidden="true">
          {collapsed ? "▸" : "▾"}
        </span>
        <span className="mount-switcher__label">Reading from extra snapshots</span>
        {mounts.length > 0 ? (
          <span className="mount-switcher__count">{mounts.length} mounted</span>
        ) : (
          <span className="mount-switcher__count">none</span>
        )}
      </button>

      {collapsed ? null : (
        <>
      {mounts.length > 0 ? (
        <ul className="mount-switcher__chips">
          {mounts.map((m) => {
            const key = mountKey(m.sourceUser, m.snapshot);
            return (
              <li key={key} className="mount-chip" title={`${m.count} nodes — read-only overlay`}>
                <span className="mount-chip__name">{m.snapshot}</span>
                {!m.isSelf ? (
                  <span className="mount-chip__owner">from {m.sourceUser}</span>
                ) : null}
                <button
                  type="button"
                  className="mount-chip__remove"
                  onClick={() => remove(m)}
                  disabled={pending.has(key)}
                  aria-label={`Stop reading from ${m.snapshot}`}
                  title="Stop reading from this snapshot"
                >
                  {pending.has(key) ? "…" : "×"}
                </button>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="mount-switcher__empty">
          Reading only your live graph. Search below to add extra snapshots.
        </p>
      )}

      <div className="mount-switcher__search">
        <input
          id="mount-switcher-search"
          className="mount-switcher__input"
          type="text"
          placeholder="Search snapshots to add…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onFocus={handleFocus}
          onBlur={handleBlur}
          autoComplete="off"
        />
        {open ? (
          <ul className="mount-switcher__results">
            {results.length === 0 ? (
              <li className="mount-result mount-result--empty">
                {options.length === 0 ? "No snapshots available" : "No matches"}
              </li>
            ) : (
              results.map((o) => {
                const mounted = mountedKeys.has(o.key);
                const isPending = pending.has(o.key);
                return (
                  <li key={o.key}>
                    <button
                      type="button"
                      className={`mount-result${mounted ? " mount-result--added" : ""}`}
                      // Keep focus on the input so the list stays open across clicks.
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => toggle(o)}
                      disabled={isPending}
                    >
                      <span className="mount-result__label">
                        <span className="mount-result__name">{o.snapshot}</span>
                        <span className="mount-result__meta">
                          {o.owner} · {o.count} node{o.count === 1 ? "" : "s"}
                        </span>
                      </span>
                      <span className="mount-result__action" aria-hidden="true">
                        {isPending ? "…" : mounted ? "✓ added" : "+ add"}
                      </span>
                    </button>
                  </li>
                );
              })
            )}
          </ul>
        ) : null}
      </div>

      {error ? <p className="mount-switcher__error">{error}</p> : null}
        </>
      )}
    </section>
  );
}
