import { usePhoenixTraces } from "../hooks/usePhoenixTraces";
import type { DataSourceMode } from "../config/dataSource";
import type { Candidate } from "../types/candidate";
import type { PhoenixTrace } from "../types/phoenix";

interface PhoenixTracesProps {
  candidate: Candidate;
  mode: DataSourceMode;
}

function formatMs(value: number | null): string {
  if (value == null) {
    return "—";
  }
  if (value >= 1000) {
    return `${(value / 1000).toFixed(2)} s`;
  }
  return `${Math.round(value)} ms`;
}

function formatTokens(trace: PhoenixTrace): string {
  const { prompt, completion, total } = trace.tokens;
  if (total == null && prompt == null && completion == null) {
    return "—";
  }
  const parts: string[] = [];
  if (prompt != null) {
    parts.push(`${prompt} in`);
  }
  if (completion != null) {
    parts.push(`${completion} out`);
  }
  const detail = parts.length ? ` (${parts.join(" · ")})` : "";
  return `${total ?? "—"}${detail}`;
}

function statusClass(statusCode: string | null): string {
  const value = (statusCode ?? "").toUpperCase();
  if (value === "OK") {
    return "phoenix-status phoenix-status--ok";
  }
  if (value === "ERROR") {
    return "phoenix-status phoenix-status--error";
  }
  return "phoenix-status";
}

function TraceCard({ trace }: { trace: PhoenixTrace }) {
  return (
    <li className="phoenix-trace">
      <div className="phoenix-trace__head">
        <code className="mono small">{trace.traceId || "(no id)"}</code>
        {trace.statusCode ? (
          <span className={statusClass(trace.statusCode)}>{trace.statusCode}</span>
        ) : null}
      </div>
      <dl className="phoenix-trace__meta">
        <div>
          <dt>Latency</dt>
          <dd className="mono">{formatMs(trace.latencyMs)}</dd>
        </div>
        <div>
          <dt>Tokens</dt>
          <dd className="mono">{formatTokens(trace)}</dd>
        </div>
        <div>
          <dt>Model</dt>
          <dd>{trace.model ?? "—"}</dd>
        </div>
        <div>
          <dt>Spans</dt>
          <dd className="mono">{trace.spanCount ?? trace.spans.length}</dd>
        </div>
      </dl>
      {trace.spans.length > 0 ? (
        <details className="phoenix-trace__spans">
          <summary>{trace.spans.length} spans</summary>
          <ul>
            {trace.spans.map((span, index) => (
              <li key={`${span.name}-${index}`}>
                <span className="phoenix-span__kind">{span.kind}</span>
                <span className="phoenix-span__name">{span.name}</span>
                <span className="mono small">{formatMs(span.latencyMs)}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {trace.phoenixUrl ? (
        <a
          className="phoenix-trace__link"
          href={trace.phoenixUrl}
          target="_blank"
          rel="noreferrer"
          aria-label={`Open Phoenix spans for trace ${trace.traceId}`}
          title={`Open Phoenix spans for trace ${trace.traceId}`}
        >
          Open in Phoenix
          <span aria-hidden="true"> ↗</span>
        </a>
      ) : null}
    </li>
  );
}

/**
 * Phoenix trace context for the selected candidate. Reads the candidate's
 * Phoenix identifier from `extra` and resolves traces via the proxy (live) or
 * the mock fixture (mock/local-logs).
 */
export function PhoenixTraces({ candidate, mode }: PhoenixTracesProps) {
  const { link, linked, traces, loading, error, unconfigured } =
    usePhoenixTraces(candidate, mode);

  return (
    <div className="detail-section" aria-labelledby="detail-phoenix-heading">
      <h4 id="detail-phoenix-heading">Phoenix traces</h4>

      {!linked ? (
        <p className="muted">
          No Phoenix trace linked to this candidate yet. Dominic&apos;s eval
          pipeline attaches a trace id once the candidate is exercised.
        </p>
      ) : loading ? (
        <p className="muted" role="status">
          Loading Phoenix traces…
        </p>
      ) : unconfigured ? (
        <p className="muted" role="status">
          Phoenix trace context isn&apos;t configured for this environment yet.
        </p>
      ) : error ? (
        <p className="error-text" role="alert">
          Couldn&apos;t load Phoenix traces: {error}
        </p>
      ) : traces.length === 0 ? (
        <p className="muted">
          No Phoenix traces found for{" "}
          <code className="mono small">
            {link.traceId ?? link.sessionId}
          </code>
          .
        </p>
      ) : (
        <ul className="phoenix-trace-list">
          {traces.map((trace) => (
            <TraceCard key={trace.traceId} trace={trace} />
          ))}
        </ul>
      )}
    </div>
  );
}
