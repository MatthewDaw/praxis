import { useEffect, useMemo, useState } from "react";
import {
  fetchPhoenixTraces,
  hasPhoenixLink,
  phoenixLinkFromExtra,
  PhoenixUnconfiguredError,
} from "../api/phoenixClient";
import type { DataSourceMode } from "../config/dataSource";
import type { Candidate } from "../types/candidate";
import type { PhoenixLink, PhoenixTrace } from "../types/phoenix";

export interface UsePhoenixTracesResult {
  link: PhoenixLink;
  linked: boolean;
  traces: PhoenixTrace[];
  loading: boolean;
  /** Human-readable error, or null. */
  error: string | null;
  /** True when the proxy/project key is not configured (distinct from failure). */
  unconfigured: boolean;
}

/**
 * Resolve the Phoenix trace(s) tied to a candidate. Re-fetches when the
 * candidate or data-source mode changes; stale responses are ignored.
 */
export function usePhoenixTraces(
  candidate: Candidate | null,
  mode: DataSourceMode,
): UsePhoenixTracesResult {
  const link = useMemo<PhoenixLink>(
    () => (candidate ? phoenixLinkFromExtra(candidate.extra) : {}),
    [candidate],
  );
  const linked = hasPhoenixLink(link);

  const [traces, setTraces] = useState<PhoenixTrace[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unconfigured, setUnconfigured] = useState(false);

  const linkKey = `${link.traceId ?? ""}|${link.sessionId ?? ""}|${link.project ?? ""}`;

  useEffect(() => {
    if (!linked) {
      setTraces([]);
      setLoading(false);
      setError(null);
      setUnconfigured(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);
    setUnconfigured(false);

    fetchPhoenixTraces(link, mode)
      .then((response) => {
        if (cancelled) {
          return;
        }
        setTraces(response.traces);
      })
      .catch((err: unknown) => {
        if (cancelled) {
          return;
        }
        setTraces([]);
        if (err instanceof PhoenixUnconfiguredError) {
          setUnconfigured(true);
        } else {
          setError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
    // linkKey captures the identifier triple; mode toggles the source.
  }, [linkKey, mode, linked]); // eslint-disable-line react-hooks/exhaustive-deps

  return { link, linked, traces, loading, error, unconfigured };
}
