import { useCallback, useState } from "react";
import {
  buildConfigFromPreset,
  configDetail,
  getPresetById,
  persistConfig,
  PRESET_IDS,
  resolveInitialConfig,
  type DataSourceConfig,
  type DataSourceMode,
} from "../config/dataSource";
import type { ApiDataProviderAuth } from "../api/apiClient";
import type { ParsedLogSession } from "../types/transcript";

export function useDataSource(
  localSession?: ParsedLogSession | null,
  auth?: ApiDataProviderAuth,
) {
  const [config, setConfig] = useState<DataSourceConfig>(() => resolveInitialConfig());

  const applyConfig = useCallback((presetId: string, customApiBaseUrl?: string) => {
    const next = buildConfigFromPreset(presetId, customApiBaseUrl);
    persistConfig(next);
    setConfig(next);
    return next;
  }, []);

  const mode: DataSourceMode = config.mode;
  const label = config.label;
  const detail = configDetail(config, localSession);
  const apiUrl = config.mode === "live" ? config.apiBaseUrl : undefined;
  const ingestApiBaseUrl =
    import.meta.env.VITE_PRAXIS_API_BASE_URL?.trim() ||
    getPresetById(PRESET_IDS.local)?.defaultApiBaseUrl;

  return {
    config,
    mode,
    label,
    detail,
    apiUrl,
    ingestApiBaseUrl,
    auth,
    applyConfig,
  };
}
