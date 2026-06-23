import { useEffect, useRef, useState } from "react";
import type { ViewTab } from "../../types/view";

interface FilterBarProps {
  searchQuery: string;
  stateFilter: string;
  viewTab: ViewTab;
  candidateCount: number;
  contradictionCount: number;
  onSearchChange: (value: string) => void;
  onStateFilterChange: (value: string) => void;
  onViewTabChange: (tab: ViewTab) => void;
  onAddEval?: () => void;
}

export function FilterBar({
  searchQuery,
  stateFilter,
  viewTab,
  candidateCount,
  contradictionCount,
  onSearchChange,
  onStateFilterChange,
  onViewTabChange,
  onAddEval,
}: FilterBarProps) {
  const [showViewMenu, setShowViewMenu] = useState(false);
  const viewMenuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (viewMenuRef.current && !viewMenuRef.current.contains(event.target as Node)) {
        setShowViewMenu(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const isViewTab = viewTab === "table" || viewTab === "cards" || viewTab === "graph";

  return (
    <section className="filter-bar" aria-label="Candidate filters">
      <div className="filter-bar__fields" hidden={viewTab === "setup"}>
        <label className="filter-field">
          Search
          <span className="filter-field__hint">Title or content</span>
          <input
            type="search"
            placeholder="Search by title or content..."
            value={searchQuery}
            onChange={(event) => onSearchChange(event.target.value)}
            aria-label="Search candidates"
          />
        </label>
        <label className="filter-field">
          Filter by state
          <select
            value={stateFilter}
            onChange={(event) => onStateFilterChange(event.target.value)}
            aria-label="Filter by state"
          >
            <option>All</option>
            <option>proposed</option>
            <option>active</option>
            <option>decayed</option>
          </select>
        </label>
      </div>
      <div className="filter-bar__controls">
        {onAddEval && viewTab !== "setup" ? (
          <button type="button" className="btn secondary" onClick={onAddEval}>
            Add eval
          </button>
        ) : null}
        {viewTab !== "setup" ? (
          <span className="count-chip" aria-live="polite">
            {candidateCount} candidates
          </span>
        ) : null}
        <div className="view-toggle" role="tablist" aria-label="View mode">
          <div ref={viewMenuRef} className="view-toggle__group">
            <button
              type="button"
              role="tab"
              className={isViewTab ? "view-toggle__tab active" : "view-toggle__tab"}
              aria-selected={isViewTab}
              aria-haspopup="true"
              aria-expanded={showViewMenu}
              onClick={() => setShowViewMenu((prev) => !prev)}
            >
              View ▾
            </button>
            {showViewMenu && (
              <div className="view-toggle__menu" role="menu">
                {(
                  [
                    { tab: "table", label: "Table view" },
                    { tab: "cards", label: "Card view" },
                    { tab: "graph", label: "Graph" },
                  ] as const
                ).map(({ tab, label }) => (
                  <button
                    key={tab}
                    type="button"
                    role="menuitem"
                    className={viewTab === tab ? "view-toggle__menu-item active" : "view-toggle__menu-item"}
                    onClick={() => {
                      onViewTabChange(tab);
                      setShowViewMenu(false);
                    }}
                  >
                    {label}
                  </button>
                ))}
              </div>
            )}
          </div>
          <button
            type="button"
            role="tab"
            className={
              viewTab === "contradictions" ? "view-toggle__tab active" : "view-toggle__tab"
            }
            aria-selected={viewTab === "contradictions"}
            onClick={() => onViewTabChange("contradictions")}
          >
            Contradictions
            {contradictionCount > 0 ? ` (${contradictionCount})` : ""}
          </button>
          <button
            type="button"
            role="tab"
            className={viewTab === "setup" ? "view-toggle__tab active" : "view-toggle__tab"}
            aria-selected={viewTab === "setup"}
            onClick={() => onViewTabChange("setup")}
          >
            MCP Setup
          </button>
        </div>
      </div>
    </section>
  );
}
