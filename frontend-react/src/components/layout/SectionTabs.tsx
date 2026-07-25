import type { ViewTab } from "../../types/view";

interface SectionTabsProps {
  viewTab: ViewTab;
  contradictionCount: number;
  onViewTabChange: (tab: ViewTab) => void;
  /**
   * Server-side kill switch (`GET /productivity`, `{status}`). `false` while
   * unknown (still loading) or explicitly disabled — the tab stays hidden
   * either way so a leaked/revoked GitHub token can be contained without a
   * redeploy and the dashboard never flashes a tab that will 404/disable.
   */
  productivityEnabled: boolean;
}

export function SectionTabs({
  viewTab,
  contradictionCount,
  onViewTabChange,
  productivityEnabled,
}: SectionTabsProps) {
  const isKnowledgeView =
    viewTab === "table" || viewTab === "cards" || viewTab === "graph";

  return (
    <div className="view-toggle" role="tablist" aria-label="Dashboard section">
      <button
        type="button"
        role="tab"
        className={isKnowledgeView ? "view-toggle__tab active" : "view-toggle__tab"}
        aria-selected={isKnowledgeView}
        onClick={() => {
          if (!isKnowledgeView) {
            onViewTabChange("table");
          }
        }}
      >
        Knowledge
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
        className={viewTab === "context" ? "view-toggle__tab active" : "view-toggle__tab"}
        aria-selected={viewTab === "context"}
        onClick={() => onViewTabChange("context")}
      >
        Context
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
      {productivityEnabled ? (
        <button
          type="button"
          role="tab"
          className={
            viewTab === "productivity" ? "view-toggle__tab active" : "view-toggle__tab"
          }
          aria-selected={viewTab === "productivity"}
          onClick={() => onViewTabChange("productivity")}
        >
          Productivity
        </button>
      ) : null}
    </div>
  );
}
