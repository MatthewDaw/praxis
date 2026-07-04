import type { ViewTab } from "../../types/view";

const VIEW_TABS = [
  { tab: "table", label: "Table view" },
  { tab: "cards", label: "Card view" },
  { tab: "graph", label: "Graph" },
] as const;

interface FilterBarProps {
  searchQuery: string;
  stateFilter: string;
  viewTab: ViewTab;
  candidateCount: number;
  onSearchChange: (value: string) => void;
  onStateFilterChange: (value: string) => void;
  onViewTabChange: (tab: ViewTab) => void;
  onClearGraph?: () => void;
  /** Save the live graph into the active snapshot (prompts for a name if none). */
  onSaveSnapshot?: () => void;
  /** The active snapshot name, used for the Save button's tooltip ("" = none). */
  saveTarget?: string;
}

export function FilterBar({
  searchQuery,
  stateFilter,
  viewTab,
  candidateCount,
  onSearchChange,
  onStateFilterChange,
  onViewTabChange,
  onClearGraph,
  onSaveSnapshot,
  saveTarget,
}: FilterBarProps) {
  const isKnowledgeView = viewTab === "table" || viewTab === "cards" || viewTab === "graph";

  if (viewTab === "setup") {
    return <section className="filter-bar" aria-label="Candidate filters" hidden />;
  }

  return (
    <section className="filter-bar" aria-label="Candidate filters">
      <input
        className="filter-bar__search"
        type="search"
        placeholder="Search by title or content..."
        value={searchQuery}
        onChange={(event) => onSearchChange(event.target.value)}
        aria-label="Search candidates"
      />
      <select
        className="filter-bar__state"
        value={stateFilter}
        onChange={(event) => onStateFilterChange(event.target.value)}
        aria-label="Filter by state"
      >
        <option value="All">All states</option>
        <option value="proposed">Proposed</option>
        <option value="active">Approved</option>
        <option value="rejected">Rejected</option>
      </select>
      {isKnowledgeView ? (
        <div
          className="knowledge-view-toggle"
          role="tablist"
          aria-label="Knowledge view mode"
        >
          {VIEW_TABS.map(({ tab, label }) => (
            <button
              key={tab}
              type="button"
              role="tab"
              className={
                viewTab === tab
                  ? "knowledge-view-toggle__tab active"
                  : "knowledge-view-toggle__tab"
              }
              aria-selected={viewTab === tab}
              onClick={() => onViewTabChange(tab)}
            >
              {label}
            </button>
          ))}
        </div>
      ) : null}
      <div className="filter-bar__controls">
        {onSaveSnapshot ? (
          <button
            type="button"
            className="btn primary"
            onClick={onSaveSnapshot}
            title={
              saveTarget
                ? `Save the live graph into "${saveTarget}"`
                : "Save the live graph as a new snapshot"
            }
          >
            Save
          </button>
        ) : null}
        {onClearGraph ? (
          <button type="button" className="btn delete" onClick={onClearGraph}>
            Truncate graph
          </button>
        ) : null}
        <span className="count-chip" aria-live="polite">
          {candidateCount} candidates
        </span>
      </div>
    </section>
  );
}
