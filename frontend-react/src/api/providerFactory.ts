import type { DataProvider } from "./dataProvider";
import type { DataSourceConfig } from "../config/dataSource";
import type { ParsedLogSession } from "../types/transcript";
import { createApiDataProvider, type ApiDataProviderAuth } from "./apiClient";
import {
  createEmptyLocalLogsProvider,
  createLocalLogsDataProvider,
} from "./localLogsProvider";
import { createMockDataProvider } from "./mockProvider";

export function resolveDataProvider(
  config: DataSourceConfig,
  localSession?: ParsedLogSession | null,
  auth?: ApiDataProviderAuth,
): DataProvider {
  if (config.mode === "local-logs") {
    if (localSession && localSession.lines.length > 0) {
      return createLocalLogsDataProvider(localSession);
    }
    return createEmptyLocalLogsProvider();
  }

  if (config.mode === "mock") {
    return createMockDataProvider(config.evalMetricsUrl, config.apiToken);
  }

  if (!config.apiBaseUrl) {
    return createMockDataProvider(config.evalMetricsUrl, config.apiToken);
  }

  return createApiDataProvider(
    config.apiBaseUrl,
    auth ?? { getToken: async () => config.apiToken },
    config.evalMetricsUrl,
  );
}

/** @deprecated Use resolveDataProvider with DataSourceConfig */
export function getDataProvider(): DataProvider {
  const baseUrl = import.meta.env.VITE_PRAXIS_API_BASE_URL?.trim();
  if (baseUrl) {
    const token = import.meta.env.VITE_PRAXIS_API_TOKEN?.trim();
    return createApiDataProvider(baseUrl, { getToken: async () => token || undefined });
  }
  return createMockDataProvider();
}
