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
          <button
            type="button"
            role="tab"
            className={viewTab === "table" ? "view-toggle__tab active" : "view-toggle__tab"}
            aria-selected={viewTab === "table"}
            onClick={() => onViewTabChange("table")}
          >
            Table view
          </button>
          <button
            type="button"
            role="tab"
            className={viewTab === "cards" ? "view-toggle__tab active" : "view-toggle__tab"}
            aria-selected={viewTab === "cards"}
            onClick={() => onViewTabChange("cards")}
          >
            Card view
          </button>
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
            className={viewTab === "graph" ? "view-toggle__tab active" : "view-toggle__tab"}
            aria-selected={viewTab === "graph"}
            onClick={() => onViewTabChange("graph")}
          >
            Graph
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
