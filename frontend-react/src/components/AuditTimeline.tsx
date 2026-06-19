import type { AuditEntry } from "../types/candidate";

interface AuditTimelineProps {
  entries: AuditEntry[];
}

export function AuditTimeline({ entries }: AuditTimelineProps) {
  return (
    <ul className="audit-timeline">
      {entries.map((entry, index) => (
        <li key={`${entry.action}-${entry.timestamp}-${index}`} className="audit-timeline__item">
          <div className="audit-timeline__action">{entry.action}</div>
          <div className="audit-timeline__meta">
            {entry.timestamp} · <code>{entry.provenance}</code> · <em>{entry.actor}</em>
            {entry.note ? ` — ${entry.note}` : ""}
          </div>
        </li>
      ))}
    </ul>
  );
}
