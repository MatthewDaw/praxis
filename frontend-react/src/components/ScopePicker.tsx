import { useMemo, useState } from "react";
import type { EvalScope } from "../api/apiClient";

// Parent folder of a scope path ("matt/applications" -> "matt", "matt" -> ".").
export function parentScope(scope: string): string | null {
  if (scope === ".") return null;
  const i = scope.lastIndexOf("/");
  return i === -1 ? "." : scope.slice(0, i);
}

export function leafName(scope: string): string {
  const i = scope.lastIndexOf("/");
  return i === -1 ? scope : scope.slice(i + 1);
}

interface ScopePickerProps {
  scopes: EvalScope[];
  selected: string[];
  onChange: (next: string[]) => void;
  /** Cached case ids → cached node count. Membership = cached (green dot). */
  cached?: Map<string, number>;
}

/** A file-tree style folder browser with multi-select (folders and/or cases). */
export function ScopePicker({ scopes, selected, onChange, cached }: ScopePickerProps) {
  const [scope, setScope] = useState("."); // navigation cursor (current folder)

  const currentCount = useMemo(
    () => scopes.find((s) => s.scope === scope)?.caseCount ?? 0,
    [scopes, scope],
  );
  // A scope is a navigable directory if some other scope sits beneath it.
  const dirSet = useMemo(() => {
    const set = new Set<string>();
    for (const s of scopes) {
      const p = parentScope(s.scope);
      if (p) set.add(p);
    }
    return set;
  }, [scopes]);
  const childFolders = useMemo(
    () =>
      scopes
        .filter((s) => parentScope(s.scope) === scope)
        .sort((a, b) => {
          const ad = dirSet.has(a.scope) ? 0 : 1;
          const bd = dirSet.has(b.scope) ? 0 : 1;
          return ad - bd || a.scope.localeCompare(b.scope);
        }),
    [scopes, scope, dirSet],
  );
  const breadcrumb = useMemo(() => {
    const crumbs = [{ path: ".", label: "all cases" }];
    if (scope !== ".") {
      let acc = "";
      for (const seg of scope.split("/")) {
        acc = acc ? `${acc}/${seg}` : seg;
        crumbs.push({ path: acc, label: seg });
      }
    }
    return crumbs;
  }, [scope]);

  function toggle(path: string) {
    onChange(selected.includes(path) ? selected.filter((p) => p !== path) : [...selected, path]);
  }

  return (
    <>
      <div className="eval-runner__browser">
        <nav className="eval-runner__crumbs" aria-label="Scope folder">
          {breadcrumb.map((c, i) => (
            <span key={c.path} className="eval-runner__crumb-item">
              {i > 0 ? <span className="eval-runner__crumb-sep">/</span> : null}
              <button
                type="button"
                className={`eval-runner__crumb${c.path === scope ? " is-current" : ""}`}
                onClick={() => setScope(c.path)}
                disabled={c.path === scope}
              >
                {c.label}
              </button>
            </span>
          ))}
          <label className="eval-runner__crumb-count eval-runner__select-here">
            <input
              type="checkbox"
              checked={selected.includes(scope)}
              onChange={() => toggle(scope)}
            />
            select this folder ({currentCount})
          </label>
        </nav>

        <ul className="eval-runner__list-folders">
          {childFolders.length === 0 ? (
            <li className="eval-runner__folders-empty">
              No subfolders — check &ldquo;select this folder&rdquo; above to queue its{" "}
              {currentCount} case{currentCount === 1 ? "" : "s"}.
            </li>
          ) : (
            childFolders.map((c) => {
              const isDir = dirSet.has(c.scope);
              const checked = selected.includes(c.scope);
              return (
                <li key={c.scope} className={`eval-runner__entry${checked ? " is-checked" : ""}`}>
                  <input
                    type="checkbox"
                    className="eval-runner__entry-check"
                    checked={checked}
                    onChange={() => toggle(c.scope)}
                    aria-label={`Select ${c.scope}`}
                  />
                  <button
                    type="button"
                    className={`eval-runner__entry-main${isDir ? " is-dir" : ""}`}
                    onClick={() => (isDir ? setScope(c.scope) : toggle(c.scope))}
                    title={isDir ? `Open ${c.scope}` : `Select ${c.scope}`}
                  >
                    <span className="eval-runner__entry-icon" aria-hidden>
                      {isDir ? "📁" : "📄"}
                    </span>
                    <span className="eval-runner__entry-name">{leafName(c.scope)}</span>
                    {!isDir ? (
                      // The cache is keyed by the bare case id (eval:<case_id>),
                      // which is the path's leaf — not the full folder path. Match
                      // on leafName so nested cases (e.g. matt/foo) light up too.
                      (() => {
                        const isCached = cached?.has(leafName(c.scope)) ?? false;
                        // Not cached → keep the plain red dot (no count). Cached →
                        // a green circle showing the cached node count.
                        if (!isCached) {
                          return (
                            <span
                              className="eval-runner__cache-dot"
                              title="not cached"
                              aria-label="not cached"
                            />
                          );
                        }
                        const nodes = cached?.get(leafName(c.scope)) ?? 0;
                        return (
                          <span
                            className="eval-runner__node-badge is-cached"
                            title={`${nodes} node${nodes === 1 ? "" : "s"} · cached`}
                            aria-label={`${nodes} node${nodes === 1 ? "" : "s"}, cached`}
                          >
                            {nodes}
                          </span>
                        );
                      })()
                    ) : null}
                    {isDir ? <span className="eval-runner__entry-count">{c.caseCount}</span> : null}
                    {isDir ? <span className="eval-runner__entry-caret">›</span> : null}
                  </button>
                </li>
              );
            })
          )}
        </ul>
      </div>

      {selected.length ? (
        <div className="eval-runner__selected">
          <span className="eval-runner__selected-label">Selected ({selected.length}):</span>
          {selected.map((s) => (
            <button
              key={s}
              type="button"
              className="eval-runner__pill"
              onClick={() => toggle(s)}
              title="Remove from selection"
            >
              {s === "." ? "all cases" : s} ✕
            </button>
          ))}
          <button type="button" className="eval-runner__clear" onClick={() => onChange([])}>
            clear
          </button>
        </div>
      ) : null}
    </>
  );
}
