type ViewTab = "table" | "cards";

interface FilterBarProps {
  searchQuery: string;
  stateFilter: string;
  viewTab: ViewTab;
  candidateCount: number;
  onSearchChange: (value: string) => void;
  onStateFilterChange: (value: string) => void;
  onViewTabChange: (tab: ViewTab) => void;
}

export function FilterBar({
  searchQuery,
  stateFilter,
  viewTab,
  candidateCount,
  onSearchChange,
  onStateFilterChange,
  onViewTabChange,
}: FilterBarProps) {
  return (
    <section className="filter-bar" aria-label="Candidate filters">
      <div className="filter-bar__fields">
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
            <option>suggested</option>
            <option>active</option>
            <option>decayed</option>
          </select>
        </label>
      </div>
      <div className="filter-bar__controls">
        <span className="count-chip" aria-live="polite">
          {candidateCount} candidates
        </span>
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
        </div>
      </div>
    </section>
  );
}
