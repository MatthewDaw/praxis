import { useCallback, useEffect, useState } from "react";
import type { DataSourceConfig } from "../config/dataSource";

export type ApiStoreType = "postgres" | "json";

export interface ApiHealthState {
  storeType: ApiStoreType | undefined;
  candidateCount: number | undefined;
  loading: boolean;
  error: string | undefined;
}

export function useApiHealth(
  config: DataSourceConfig,
  refreshKey = 0,
): ApiHealthState & { refetch: () => void } {
  const [storeType, setStoreType] = useState<ApiStoreType | undefined>();
  const [candidateCount, setCandidateCount] = useState<number | undefined>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [tick, setTick] = useState(0);

  const refetch = useCallback(() => {
    setTick((value) => value + 1);
  }, []);

  useEffect(() => {
    if (config.mode !== "live" || !config.apiBaseUrl) {
      setStoreType(undefined);
      setCandidateCount(undefined);
      setLoading(false);
      setError(undefined);
      return;
    }

    const controller = new AbortController();
    let cancelled = false;

    async function fetchHealth() {
      setLoading(true);
      setError(undefined);
      try {
        const response = await fetch(
          `${config.apiBaseUrl!.replace(/\/$/, "")}/health`,
          {
            headers: { Accept: "application/json" },
            signal: controller.signal,
          },
        );
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        const payload = (await response.json()) as {
          store?: string;
          candidates?: number;
        };
        if (cancelled) {
          return;
        }
        const nextStore =
          payload.store === "postgres" || payload.store === "json"
            ? payload.store
            : undefined;
        setStoreType(nextStore);
        setCandidateCount(
          typeof payload.candidates === "number" ? payload.candidates : undefined,
        );
      } catch (err) {
        if (cancelled || (err instanceof DOMException && err.name === "AbortError")) {
          return;
        }
        setStoreType(undefined);
        setCandidateCount(undefined);
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void fetchHealth();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [config.mode, config.apiBaseUrl, refreshKey, tick]);

  return { storeType, candidateCount, loading, error, refetch };
}
