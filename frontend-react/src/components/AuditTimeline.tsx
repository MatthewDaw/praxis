import { useState } from "react";
import type { AuditEntry } from "../types/candidate";

interface AuditTimelineProps {
  entries: AuditEntry[];
  /** Entries rendered while collapsed. The rest appear behind the expand control. */
  previewCount?: number;
}

const DEFAULT_PREVIEW = 3;

/**
 * A fact's provenance, oldest first.
 *
 * Trails run long — tickets carried 40+ entries before compaction — so this collapses to
 * the first few by default. The COUNT is always stated: a trail whose visible portion is
 * truncated must never read as a short trail, which is how a whole history going missing
 * stayed invisible until someone byte-diffed a pre-move dump. For the same reason `note`
 * is rendered whenever present — the `compacted` marker's note is the only record that
 * history was deliberately dropped, and hiding it makes a compacted trail and a genuinely
 * short one look identical.
 */
export function AuditTimeline({ entries, previewCount = DEFAULT_PREVIEW }: AuditTimelineProps) {
  const [expanded, setExpanded] = useState(false);
  const hidden = Math.max(0, entries.length - previewCount);
  const collapsed = hidden > 0 && !expanded;
  const shown = collapsed ? entries.slice(0, previewCount) : entries;

  return (
    <div className="audit-timeline-wrap">
      <p className="audit-timeline__count muted">
        {entries.length} {entries.length === 1 ? "entry" : "entries"}
        {collapsed ? ` · showing ${shown.length}` : ""}
      </p>
      <ol className="audit-timeline">
        {shown.map((entry, index) => (
          <li key={`${entry.action}-${entry.timestamp}-${index}`} className="audit-timeline__item">
            <div className="audit-timeline__action">{entry.action}</div>
            <div className="audit-timeline__meta">
              {entry.timestamp || "no timestamp"} ·{" "}
              <code>{entry.provenance || "no provenance"}</code> · <em>{entry.actor}</em>
            </div>
            {entry.note ? <div className="audit-timeline__note">{entry.note}</div> : null}
            {Object.keys(entry.extra ?? {}).length > 0 ? (
              <div className="audit-timeline__extra">
                {Object.entries(entry.extra ?? {}).map(([key, value]) => (
                  <span key={key}>
                    {key}: <code>{typeof value === "string" ? value : JSON.stringify(value)}</code>
                  </span>
                ))}
              </div>
            ) : null}
          </li>
        ))}
      </ol>
      {hidden > 0 ? (
        <button
          type="button"
          className="btn secondary audit-timeline__toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded
            ? "Collapse audit trail"
            : `Show all ${entries.length} entries (${hidden} hidden)`}
        </button>
      ) : null}
    </div>
  );
}
